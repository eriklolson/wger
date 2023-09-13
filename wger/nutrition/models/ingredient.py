#  This file is part of wger Workout Manager <https://github.com/wger-project>.
#  Copyright (C) 2013 - 2021 wger Team
#
#  wger Workout Manager is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  wger Workout Manager is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Standard Library
import logging
import uuid as uuid
from decimal import Decimal

# Django
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.contrib.sites.models import Site
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
)
from django.db import models
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

# Third Party
import openfoodfacts

# wger
from wger.core.models import Language
from wger.nutrition.consts import ENERGY_FACTOR
from wger.nutrition.models.sources import Source
from wger.utils.cache import cache_mapper
from wger.utils.constants import (
    OFF_SEARCH_PRODUCT_FOUND,
    TWOPLACES,
)
from wger.utils.language import load_language
from wger.utils.managers import SubmissionManager
from wger.utils.models import (
    AbstractLicenseModel,
    AbstractSubmissionModel,
)

# Local
from .ingredient_category import IngredientCategory

logger = logging.getLogger(__name__)


class Ingredient(AbstractSubmissionModel, AbstractLicenseModel, models.Model):
    """
    An ingredient, with some approximate nutrition values
    """
    objects = SubmissionManager()
    """Custom manager"""

    ENERGY_APPROXIMATION = 15
    """
    How much the calculated energy from protein, etc. can deviate from the
    energy amount given (in percent).
    """

    language = models.ForeignKey(
        Language,
        verbose_name=_('Language'),
        editable=False,
        on_delete=models.CASCADE,
    )

    created = models.DateTimeField(
        _('Date'),
        auto_now_add=True,
    )
    """Date when the ingredient was created"""

    last_update = models.DateTimeField(
        _('Date'),
        auto_now=True,
        blank=True,
        editable=False,
    )
    """Last update time"""

    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name='UUID',
    )
    """Globally unique ID, to identify the ingredient across installations"""

    # Product infos
    name = models.CharField(
        max_length=200,
        verbose_name=_('Name'),
        validators=[MinLengthValidator(3)],
    )

    energy = models.IntegerField(
        verbose_name=_('Energy'),
        help_text=_('In kcal per 100g'),
    )

    protein = models.DecimalField(
        decimal_places=3,
        max_digits=6,
        verbose_name=_('Protein'),
        help_text=_('In g per 100g of product'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    carbohydrates = models.DecimalField(
        decimal_places=3,
        max_digits=6,
        verbose_name=_('Carbohydrates'),
        help_text=_('In g per 100g of product'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    carbohydrates_sugar = models.DecimalField(
        decimal_places=3,
        max_digits=6,
        blank=True,
        null=True,
        verbose_name=_('Sugar content in carbohydrates'),
        help_text=_('In g per 100g of product'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    fat = models.DecimalField(
        decimal_places=3,
        max_digits=6,
        verbose_name=_('Fat'),
        help_text=_('In g per 100g of product'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    fat_saturated = models.DecimalField(
        decimal_places=3,
        max_digits=6,
        blank=True,
        null=True,
        verbose_name=_('Saturated fat content in fats'),
        help_text=_('In g per 100g of product'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    fibres = models.DecimalField(
        decimal_places=3,
        max_digits=6,
        blank=True,
        null=True,
        verbose_name=_('Fibres'),
        help_text=_('In g per 100g of product'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    sodium = models.DecimalField(
        decimal_places=3,
        max_digits=6,
        blank=True,
        null=True,
        verbose_name=_('Sodium'),
        help_text=_('In g per 100g of product'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    code = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        db_index=True,
    )
    """Internal ID of the source database, e.g. a barcode or similar"""

    source_name = models.CharField(
        max_length=200,
        null=True,
        blank=True,
    )
    """Name of the source, such as Open Food Facts"""

    source_url = models.URLField(
        verbose_name=_('Link'),
        help_text=_('Link to product'),
        blank=True,
        null=True,
    )
    """URL of the product at the source"""

    last_imported = models.DateTimeField(
        _('Date'),
        auto_now_add=True,
        null=True,
        blank=True,
    )

    common_name = models.CharField(
        max_length=200,
        null=True,
        blank=True,
    )

    category = models.ForeignKey(
        IngredientCategory,
        verbose_name=_('Category'),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    brand = models.CharField(
        max_length=200,
        verbose_name=_('Brand name of product'),
        null=True,
        blank=True,
    )

    search_column = SearchVectorField(null=True)
    """Column used for full text search"""

    # Metaclass to set some other properties
    class Meta:
        ordering = [
            "name",
        ]
        indexes = (GinIndex(fields=["search_column"]),)

    #
    # Django methods
    #

    def get_absolute_url(self):
        """
        Returns the canonical URL to view this object.

        Since some names consist of only non-ascii characters (e.g. 감자깡), the
        resulting slug would be empty and no URL would match. In that case, use
        the regular URL with only the ID.
        """
        slug = slugify(self.name)
        if not slug:
            return reverse('nutrition:ingredient:view', kwargs={'pk': self.id})
        else:
            return reverse('nutrition:ingredient:view', kwargs={'pk': self.id, 'slug': slug})

    def clean(self):
        """
        Do a very broad sanity check on the nutritional values according to
        the following rules:
        - 1g of protein: 4kcal
        - 1g of carbohydrates: 4kcal
        - 1g of fat: 9kcal

        The sum is then compared to the given total energy, with ENERGY_APPROXIMATION
        percent tolerance.
        """

        # Note: calculations in 100 grams, to save us the '/100' everywhere
        energy_protein = 0
        if self.protein:
            energy_protein = self.protein * ENERGY_FACTOR['protein']['kg']

        energy_carbohydrates = 0
        if self.carbohydrates:
            energy_carbohydrates = self.carbohydrates * ENERGY_FACTOR['carbohydrates']['kg']

        energy_fat = 0
        if self.fat:
            # TODO: for some reason, during the tests the fat value is not
            #       converted to decimal (django 1.9)
            energy_fat = Decimal(self.fat * ENERGY_FACTOR['fat']['kg'])

        energy_calculated = energy_protein + energy_carbohydrates + energy_fat

        # Compare the values, but be generous
        if self.energy:
            energy_upper = self.energy * (1 + (self.ENERGY_APPROXIMATION / Decimal(100.0)))
            energy_lower = self.energy * (1 - (self.ENERGY_APPROXIMATION / Decimal(100.0)))

            if not ((energy_upper > energy_calculated) and (energy_calculated > energy_lower)):
                raise ValidationError(
                    _(
                        'The total energy ({energy}kcal) is not the approximate sum of the '
                        'energy provided by protein, carbohydrates and fat ({energy_calculated}kcal '
                        '+/-{energy_approx}%)'.format(
                            energy=self.energy,
                            energy_calculated=energy_calculated,
                            energy_approx=self.ENERGY_APPROXIMATION
                        )
                    )
                )

    def save(self, *args, **kwargs):
        """
        Reset the cache
        """

        super(Ingredient, self).save(*args, **kwargs)
        cache.delete(cache_mapper.get_ingredient_key(self.id))

    def __str__(self):
        """
        Return a more human-readable representation
        """
        return self.name

    def __eq__(self, other):
        """
        Compare ingredients based on their values, not like django on their PKs
        """

        logger.debug('Overwritten behaviour: comparing ingredients on values, not PK.')
        equal = True
        if isinstance(other, self.__class__):
            for i in (
                'carbohydrates',
                'carbohydrates_sugar',
                'creation_date',
                'energy',
                'fat',
                'fat_saturated',
                'fibres',
                'name',
                'protein',
                'sodium',
            ):
                if (
                    hasattr(self, i) and hasattr(other, i)
                    and (getattr(self, i, None) != getattr(other, i, None))
                ):
                    equal = False
        else:
            equal = False
        return equal

    def __hash__(self):
        """
        Define a hash function

        This is rather unnecessary, but it seems that newer versions of django
        have a problem when the __eq__ function is implemented, but not the
        __hash__ one. Returning hash(pk) is also django's default.

        :return: hash(pk)
        """
        return hash(self.pk)

    #
    # Own methods
    #

    def send_email(self, request):
        """
        Sends an email after being successfully added to the database (for user
        submitted ingredients only)
        """
        try:
            user = User.objects.get(username=self.license_author)
        except User.DoesNotExist:
            return

        if self.license_author and user.email:
            translation.activate(user.userprofile.notification_language.short_name)
            url = request.build_absolute_uri(self.get_absolute_url())
            subject = _('Ingredient was successfully added to the general database')
            context = {
                'ingredient': self.name,
                'url': url,
                'site': Site.objects.get_current().domain
            }
            message = render_to_string('ingredient/email_new.tpl', context)
            mail.send_mail(
                subject,
                message,
                settings.WGER_SETTINGS['EMAIL_FROM'], [user.email],
                fail_silently=True
            )

    def set_author(self, request):
        if request.user.has_perm('nutrition.add_ingredient'):
            self.status = Ingredient.STATUS_ACCEPTED
            if not self.license_author:
                self.license_author = request.get_host().split(':')[0]
        else:
            if not self.license_author:
                self.license_author = request.user.username

            # Send email to administrator
            subject = _('New user submitted ingredient')
            message = _(
                """The user {0} submitted a new ingredient "{1}".""".format(
                    request.user.username, self.name
                )
            )
            mail.mail_admins(subject, message, fail_silently=True)

    def get_owner_object(self):
        """
        Ingredient has no owner information
        """
        return False

    @property
    def energy_kilojoule(self):
        """
        returns kilojoules for current ingredient, 0 if energy is uninitialized
        """
        if self.energy:
            return Decimal(self.energy * 4.184).quantize(TWOPLACES)
        else:
            return 0

    @property
    def off_link(self):
        if self.source_name == Source.OPEN_FOOD_FACTS.value:
            return f'https://world.openfoodfacts.org/product/{self.code}/'

    def get_image(self, request: HttpRequest):
        """
        Returns the ingredient image

        If it is not available locally, it is fetched from Open Food Facts servers
        """

        if hasattr(self, 'image'):
            return self.image

        if not request.user.is_authenticated:
            return

        if not settings.WGER_SETTINGS['USE_CELERY']:
            logger.info('Celery deactivated, skipping retrieving ingredient image')
            return

        # Let celery fetch the image
        # wger
        from wger.nutrition.tasks import fetch_ingredient_image_task
        fetch_ingredient_image_task.delay(self.pk)

    @classmethod
    def fetch_ingredient_from_off(cls, code: str):
        """
        Searches OFF by barcode and creates a local ingredient from the result
        """
        # wger
        from wger.nutrition.off import extract_info_from_off

        result = openfoodfacts.products.get_product(code)
        if result['status'] != OFF_SEARCH_PRODUCT_FOUND:
            return None
        product = result['product']

        try:
            ingredient_data = extract_info_from_off(product, load_language(product['lang']))
        except KeyError:
            return None

        if not ingredient_data['name']:
            return

        if not ingredient_data['common_name']:
            return

        ingredient = cls(**ingredient_data)
        ingredient.save()
        return ingredient
