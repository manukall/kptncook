"""
Export recipes to Tandoor.

The Default importer reads one recipe ZIP per member of the uploaded archive.
"""

import io
import json
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import IO, Any
from zipfile import ZIP_DEFLATED, ZipFile

import httpx

from kptncook.config import get_settings
from kptncook.exporter_utils import (
    BatchExportError,
    ExportBudget,
    ExportLimitError,
    asciify_string,
    bounded_export_filename,
    get_cover,
    ingredient_name,
    get_step_text,
    publish_staged_export,
    validate_export_id,
    validate_export_batch_size,
)
from kptncook.image_download import download_image
from kptncook.ingredient_groups import iter_ingredient_groups
from kptncook.models import (
    Ingredient,
    localized_fallback,
    Recipe,
    RecipeStep,
    StepIngredient,
    StepIngredientUnit,
    scale_quantity,
)

logger = logging.getLogger(__name__)


class _ArchiveStream(io.BufferedIOBase):
    """Forward-only so ZIP headers and the central directory are each charged once."""

    def __init__(self, output: IO[bytes], budget: ExportBudget) -> None:
        self.output = output
        self.budget = budget

    def write(self, data: Any) -> int:
        self.budget.charge_output(len(data))
        return self.output.write(data)


class TandoorExporter:
    def export(
        self, recipes: list[Recipe], *, budget: ExportBudget | None = None
    ) -> list[str]:
        if not recipes:
            return []
        return [self._export_archive(recipes, budget or ExportBudget())]

    def export_recipe(
        self, recipe: Recipe, *, budget: ExportBudget | None = None
    ) -> str:
        return self._export_archive([recipe], budget or ExportBudget())

    def _export_archive(self, recipes: list[Recipe], budget: ExportBudget) -> str:
        validate_export_batch_size(len(recipes))
        for recipe in recipes:
            recipe.validate_scaled_quantities()
            validate_export_id(recipe.id.oid)
        single = len(recipes) == 1
        filename = (
            self.get_export_filename(recipes[0])
            if single
            else "kptncook-tandoor-export.zip"
        )
        scratch = None
        failed_id = "archive"
        remaining = len(recipes)
        try:
            scratch = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".kptncook-")
            outer = Path(scratch.name) / "export.zip"
            with outer.open("xb") as output:
                # The inner ZIPs already compress the JSON and cover. Store each
                # directly in the outer ZIP as it is produced.
                with ZipFile(_ArchiveStream(output, budget), "w") as archive:
                    for index, recipe in enumerate(recipes, 1):
                        failed_id = recipe.id.oid
                        remaining = len(recipes) - index
                        with archive.open(f"{index}-{failed_id}.zip", "w") as member:
                            self._write_recipe_zip(member, recipe, budget)
                    failed_id = "archive"
                output.flush()
                os.fsync(output.fileno())
            target = publish_staged_export(
                outer,
                Path.cwd(),
                stem=Path(filename).stem,
                extension=".zip",
                disambiguator=recipes[0].id.oid if single else None,
            )
        except Exception as exc:
            reason = exc
            if isinstance(exc, ExportLimitError):
                reason = ExportLimitError(
                    f"{exc} Use --budget-mib to allow a larger export."
                )
            raise BatchExportError(
                (), failed_id, remaining, reason, unpublished_count=len(recipes)
            ) from exc
        finally:
            if scratch is not None:
                try:
                    scratch.cleanup()
                except OSError:
                    with suppress(Exception):
                        logger.warning(
                            "Could not clean up Tandoor staging directory %s",
                            scratch.name,
                        )
        return target.name

    def _write_recipe_zip(
        self, member: IO[bytes], recipe: Recipe, budget: ExportBudget
    ) -> None:
        payload = self.get_recipe_payload(recipe)
        with ZipFile(member, "w", compression=ZIP_DEFLATED) as inner:
            inner.writestr(
                "recipe.json", json.dumps(payload, ensure_ascii=False, indent=2)
            )
            image = self.get_cover_image_bytes(recipe=recipe, budget=budget)
            if image is not None:
                inner.writestr("image.jpg", image)

    def get_export_filename(self, recipe: Recipe) -> str:
        """Return the preferred file name.

        The actual name may get the recipe id appended if a file with this
        name already exists (for example a second recipe with the same title).
        """
        title = (
            localized_fallback(
                recipe.localized_title, language=get_settings().kptncook_lang
            )
            or "kptncook-recipe"
        )
        return bounded_export_filename(asciify_string(title), ".zip")

    def get_cover_image_bytes(
        self, recipe: Recipe, *, budget: ExportBudget | None = None
    ) -> bytes | None:
        cover = get_cover(image_list=recipe.image_list)
        if cover is None:
            return None
        cover_url = recipe.get_image_url(api_key=get_settings().kptncook_api_key)
        if cover_url is None:
            return None
        try:
            return download_image(cover_url, follow_redirects=True, budget=budget)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.info(
                    'Cover image for "%s" not found online any more.',
                    localized_fallback(
                        recipe.localized_title, language=get_settings().kptncook_lang
                    )
                    or "kptncook-recipe",
                )
            else:
                logger.warning(
                    "While trying to fetch the cover img a HTTP error occurred: %s",
                    exc.response.status_code,
                )
            return None
        except httpx.RequestError:
            logger.warning(
                "Skipping cover image: download failed or exceeded limits",
            )
            return None

    def get_recipe_payload(self, recipe: Recipe) -> dict[str, Any]:
        recipe.validate_scaled_quantities()
        payload = {
            "name": localized_fallback(
                recipe.localized_title, language=get_settings().kptncook_lang
            )
            or "kptncook-recipe",
            "description": localized_fallback(
                recipe.author_comment, language=get_settings().kptncook_lang
            )
            or "",
            "servings": recipe.servings,
            "source_url": self.get_source_url(recipe=recipe),
            "working_time": recipe.preparation_time,
            "internal": True,
            "keywords": self.get_keywords(recipe=recipe),
            "steps": self.get_steps(recipe=recipe),
        }
        if recipe.cooking_time is not None:
            payload["waiting_time"] = recipe.cooking_time
        return payload

    def get_source_url(self, recipe: Recipe) -> str:
        source_id = recipe.uid or recipe.id.oid
        return f"https://share.kptncook.com/{source_id}"

    def get_keywords(self, recipe: Recipe) -> list[dict[str, str]]:
        names = ["kptncook", *(recipe.active_tags or [])]
        if recipe.rtype:
            names.append(recipe.rtype)
        return [{"name": name} for name in dict.fromkeys(names)]

    def get_steps(self, recipe: Recipe) -> list[dict[str, Any]]:
        steps = []
        ingredients = self.get_ingredients(recipe=recipe)
        if ingredients:
            steps.append({"instruction": "", "order": 0, "ingredients": ingredients})
        for step in recipe.steps:
            steps.append(
                {
                    "order": len(steps),
                    "instruction": get_step_text(
                        step, language=get_settings().kptncook_lang, markup=True
                    ),
                    "ingredients": self.get_step_ingredients(
                        step=step, servings=recipe.servings
                    ),
                }
            )
        return steps

    def get_step_ingredients(
        self, step: RecipeStep, servings: int = 1
    ) -> list[dict[str, Any]]:
        ingredients: list[dict[str, Any]] = []
        for ingredient in step.ingredients or []:
            payload = self.get_step_ingredient_payload(
                ingredient=ingredient, servings=servings
            )
            if payload is not None:
                payload["order"] = len(ingredients)
                ingredients.append(payload)
        return ingredients

    def get_step_ingredient_payload(
        self, ingredient: StepIngredient | None, servings: int = 1
    ) -> dict[str, Any] | None:
        if ingredient is None:
            return None
        ingredient_name = self.get_step_ingredient_name(ingredient=ingredient)
        if not ingredient_name:
            logger.debug(
                "Skipping Tandoor step ingredient without a resolvable food name."
            )
            return None
        return self._ingredient_payload(
            ingredient_name,
            self.get_step_unit_payload(unit=ingredient.unit),
            scale_quantity(ingredient.quantity, servings),
        )

    @staticmethod
    def _ingredient_payload(
        name: str | None, unit: dict[str, str] | None, amount: float | None
    ) -> dict[str, Any]:
        return {
            "food": {"name": name} if name else None,
            "unit": unit,
            "amount": amount if amount is not None else 0,
            "no_amount": amount is None,
        }

    def get_step_ingredient_name(self, ingredient: StepIngredient) -> str | None:
        return ingredient_name(ingredient.ingredient, get_settings().kptncook_lang)

    def get_step_unit_payload(
        self, unit: StepIngredientUnit | None
    ) -> dict[str, str] | None:
        if unit is None:
            return None
        for candidate in (
            unit.name,
            localized_fallback(
                unit.localized_title, language=get_settings().kptncook_lang
            ),
            localized_fallback(unit.short_title, language=get_settings().kptncook_lang),
        ):
            if candidate and candidate.strip():
                return {"name": candidate}
        return None

    def get_ingredients(self, recipe: Recipe) -> list[dict[str, Any]]:
        ingredients = []
        for group_label, group_ingredients in iter_ingredient_groups(
            recipe.ingredients
        ):
            if group_label:
                ingredients.append(self.get_group_header_payload(label=group_label))
            for ingredient in group_ingredients:
                ingredients.append(
                    self.get_ingredient_payload(
                        ingredient=ingredient, servings=recipe.servings
                    )
                )
        for order, item in enumerate(ingredients):
            item["order"] = order
        return ingredients

    def get_group_header_payload(self, label: str) -> dict[str, Any]:
        return {
            **self._ingredient_payload(None, None, None),
            "note": label,
            "is_header": True,
        }

    def get_ingredient_payload(
        self, ingredient: Ingredient, servings: int = 1
    ) -> dict[str, Any]:
        return self._ingredient_payload(
            self.get_ingredient_name(ingredient),
            self.get_unit_payload(ingredient.measure),
            scale_quantity(ingredient.quantity, servings),
        )

    def get_ingredient_name(self, ingredient: Ingredient) -> str | None:
        return ingredient_name(ingredient.ingredient, get_settings().kptncook_lang)

    def get_unit_payload(self, measure: str | None) -> dict[str, str] | None:
        if not measure or not measure.strip():
            return None
        return {"name": measure}
