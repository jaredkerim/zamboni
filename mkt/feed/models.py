"""
The feed is an assembly of items of different content types.
For ease of querying, each different content type is housed in the FeedItem
model, which also houses metadata indicating the conditions under which it
should be included. So a feed is actually just a listing of FeedItem instances
that match the user's region and carrier.

Current content types able to be attached to FeedItem:
- `FeedApp` (via the `app` field)
- `FeedBrand` (via the `brand` field)
- `FeedCollection` (via the `collection` field)
"""

import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_delete

import amo.models
from addons.models import Addon, clean_slug
from amo.decorators import use_master
from amo.models import SlugField
from addons.models import Category, Preview
from translations.fields import PurifiedField, save_signal

import mkt.carriers
import mkt.regions
from mkt.collections.fields import ColorField
from mkt.constants.feed import FEEDAPP_TYPES
from mkt.ratings.validators import validate_rating
from mkt.webapps.models import Webapp
from mkt.webapps.tasks import index_webapps

from .constants import (BRAND_LAYOUT_CHOICES, BRAND_TYPE_CHOICES,
                        COLLECTION_TYPE_CHOICES, FEED_COLOR_CHOICES)


class BaseFeedCollection(amo.models.ModelBase):
    """
    On the feed, there are a number of types of feed items that share a similar
    structure: a slug, one or more member apps with a maintained sort order,
    and a number of methods and common views for operating on those apps. This
    is a base class for those feed items, including:

    - Editorial Brands: `FeedBrand`
    - Collections: `FeedCollection`
    - Operator Shelves (future): `FeedOperatorShelf`

    A series of base classes wraps the common code for these:

    - BaseFeedCollection
    - BaseFeedCollectionMembership
    - BaseFeedCollectionSerializer
    - BaseFeedCollectionViewSet

    Subclasses of BaseFeedCollection must do a few things:
    - Define an M2M field named `_apps` with a custom through model that
      inherits from `BaseFeedCollectionMembership`.
    - Set the `membership_class` class property to the custom through model
      used by `_apps`.
    - Set the `membership_relation` class property to the name of the relation
      on the model.
    """
    _apps = None
    slug = SlugField(blank=True, max_length=30, unique=True,
                     help_text='Used in collection URLs.')

    membership_class = None
    membership_relation = None

    objects = amo.models.ManagerBase()

    class Meta:
        abstract = True
        ordering = ('-id',)

    def save(self, **kw):
        self.clean_slug()
        return super(BaseFeedCollection, self).save(**kw)

    @use_master
    def clean_slug(self):
        clean_slug(self, 'slug')

    def apps(self):
        """
        Public apps on the collection, ordered by their position in the
        CollectionMembership model.

        Use this method everytime you want to display apps for a collection to
        an user.
        """
        filters = {
            'disabled_by_user': False,
            'status': amo.STATUS_PUBLIC
        }
        return self._apps.order_by(self.membership_relation).filter(**filters)

    def add_app(self, app, order=None):
        """
        Add an app to this collection. If specified, the app will be created
        with the specified `order`. If not, it will be added to the end of the
        collection.
        """
        qs = self.membership_class.objects.filter(obj=self)

        if order is None:
            aggregate = qs.aggregate(models.Max('order'))['order__max']
            order = aggregate + 1 if aggregate is not None else 0

        rval = self.membership_class.objects.create(obj=self, app=app,
                                                    order=order)

        # Help django-cache-machine: it doesn't like many 2 many relations,
        # the cache is never invalidated properly when adding a new object.
        self.membership_class.objects.invalidate(*qs)

        index_webapps.delay([app.pk])

        return rval

    def remove_app(self, app):
        """
        Remove the passed app from this collection, returning a boolean
        indicating whether a successful deletion took place.
        """
        try:
            membership = self.membership_class.objects.get(obj=self, app=app)
        except self.membership_class.DoesNotExist:
            return False
        else:
            membership.delete()
            index_webapps.delay([app.pk])
            return True

    def set_apps(self, new_apps):
        """
        Passed a list of app IDs, will remove all existing members on the
        collection and create new ones for each of the passed apps, in order.
        """
        for app in self.apps().no_cache().values_list('pk', flat=True):
            self.remove_app(Webapp.objects.get(pk=app))
        for app in new_apps:
            self.add_app(Webapp.objects.get(pk=app))


class BaseFeedCollectionMembership(amo.models.ModelBase):
    """
    A custom `through` model is required for the M2M field `_apps` on
    subclasses of `BaseFeedCollection`. This model houses an `order` field that
    maintains the order of apps in the collection. This model serves as an
    abstract base class for the custom `through` models.

    Subclasses must:
    - Define a `ForeignKey` named `obj` that relates the app to the instance
      being put on the feed.
    """
    app = models.ForeignKey(Webapp)
    order = models.SmallIntegerField(null=True)
    obj = None

    class Meta:
        abstract = True
        ordering = ('order',)
        unique_together = ('obj', 'app',)


class FeedBrandMembership(BaseFeedCollectionMembership):
    """
    An app's membership to a `FeedBrand` class, used as the through model for
    `FeedBrand._apps`.
    """
    obj = models.ForeignKey('FeedBrand')

    class Meta(BaseFeedCollectionMembership.Meta):
        abstract = False
        db_table = 'mkt_feed_brand_membership'


class FeedBrand(BaseFeedCollection):
    """
    Model for "Editorial Brands", a special type of collection that allows
    editors to quickly create content without involving localizers by choosing
    from one of a number of predefined, prelocalized titles.
    """
    _apps = models.ManyToManyField(Webapp, through=FeedBrandMembership,
                                   related_name='app_feed_brands')
    layout = models.CharField(choices=BRAND_LAYOUT_CHOICES, max_length=30)
    type = models.CharField(choices=BRAND_TYPE_CHOICES, max_length=30)

    membership_class = FeedBrandMembership
    membership_relation = 'feedbrandmembership'

    class Meta(BaseFeedCollection.Meta):
        abstract = False
        db_table = 'mkt_feed_brand'


class FeedCollectionMembership(BaseFeedCollectionMembership):
    """
    An app's membership to a `FeedBrand` class, used as the through model for
    `FeedBrand._apps`.
    """
    obj = models.ForeignKey('FeedCollection')

    class Meta(BaseFeedCollectionMembership.Meta):
        abstract = False
        db_table = 'mkt_feed_collection_membership'


class FeedCollection(BaseFeedCollection):
    """
    Model for "Collections", a type of curated collection that allows more
    complex grouping of apps than an Editorial Brand.
    """
    _apps = models.ManyToManyField(Webapp, through=FeedCollectionMembership,
                                   related_name='app_feed_collections')
    color = models.CharField(choices=FEED_COLOR_CHOICES, max_length=7,
                             null=False)
    name = PurifiedField()
    description = PurifiedField(blank=True, null=True)
    type = models.CharField(choices=COLLECTION_TYPE_CHOICES, max_length=30,
                            null=True)

    membership_class = FeedCollectionMembership
    membership_relation = 'feedcollectionmembership'

    class Meta(BaseFeedCollection.Meta):
        abstract = False
        db_table = 'mkt_feed_collection'


class FeedApp(amo.models.ModelBase):
    """
    Model for "Custom Featured Apps", a feed item highlighting a single app
    and some additional metadata (e.g. a review or a screenshot).
    """
    app = models.ForeignKey(Webapp)
    feedapp_type = models.CharField(choices=FEEDAPP_TYPES, max_length=30)
    description = PurifiedField()
    slug = SlugField(max_length=30, unique=True)
    background_color = ColorField(null=True)

    # Optionally linked to a Preview (screenshot or video).
    preview = models.ForeignKey(Preview, null=True, blank=True)

    # Optionally linked to a pull quote.
    pullquote_attribution = models.CharField(max_length=50, null=True,
                                             blank=True)
    pullquote_rating = models.PositiveSmallIntegerField(null=True, blank=True,
        validators=[validate_rating])
    pullquote_text = PurifiedField(null=True)

    image_hash = models.CharField(default=None, max_length=8, null=True,
                                  blank=True)

    class Meta:
        db_table = 'mkt_feed_app'

    def clean(self):
        """
        Require `pullquote_text` if `pullquote_rating` or
        `pullquote_attribution` are set.
        """
        if not self.pullquote_text and (self.pullquote_rating or
                                        self.pullquote_attribution):
            raise ValidationError('Pullquote text required if rating or '
                                  'attribution is defined.')
        super(FeedApp, self).clean()

    def image_path(self):
        return os.path.join(settings.FEATURED_APP_BG_PATH,
                            str(self.pk / 1000),
                            'featured_app_%s.png' % (self.pk,))

    @property
    def has_image(self):
        return bool(self.image_hash)


class FeedItem(amo.models.ModelBase):
    """
    A thin wrapper for all items that live on the feed, including metadata
    describing the conditions that the feed item should be included in a user's
    feed.
    """
    category = models.ForeignKey(Category, null=True, blank=True)
    region = models.PositiveIntegerField(
        default=None, null=True, blank=True, db_index=True,
        choices=mkt.regions.REGIONS_CHOICES_ID)
    carrier = models.IntegerField(default=None, null=True, blank=True,
                                  choices=mkt.carriers.CARRIER_CHOICES,
                                  db_index=True)

    # Types of objects that may be contained by a feed item.
    app = models.ForeignKey(FeedApp, blank=True, null=True)
    brand = models.ForeignKey(FeedBrand, blank=True, null=True)
    collection = models.ForeignKey(FeedCollection, blank=True, null=True)

    class Meta:
        db_table = 'mkt_feed_item'


# Save translations when saving instance with translated fields.
models.signals.pre_save.connect(save_signal, sender=FeedApp,
                                dispatch_uid='feedapp_translations')
models.signals.pre_save.connect(save_signal, sender=FeedCollection,
                                dispatch_uid='feedcollection_translations')


# Delete membership instances when their apps are deleted.
def remove_deleted_app_on(cls):
    def inner(*args, **kwargs):
        instance = kwargs.get('instance')
        cls.objects.filter(app_id=instance.pk).delete()
    return inner

for cls in [FeedBrandMembership, FeedCollectionMembership]:
    post_delete.connect(remove_deleted_app_on(cls), sender=Addon,
                        dispatch_uid='apps_collections_cleanup')
