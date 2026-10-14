"""Artifact maintenance must not delete the STEP bytes of a released revision.

``purge_superseded_step_files`` keeps only the models of
``components.current_revision_id``. Opening a draft moves that pointer at once,
while ``released_revision_id`` moves only on release, so a draft that replaces
the 3D model must not let maintenance delete the model placement still serves.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.component_catalog_service_postgres import (  # noqa: E402
    ComponentCatalogPostgresService,
)
from support.postgres import CATALOG_URL, requires_scratch_postgres  # noqa: E402


ACTOR = "designer@example.com"


@requires_scratch_postgres("catalog STEP purge tests")
class SupersededStepPurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        for target, value in (
            ("app.core.config.settings.CATALOG_KLC_ENABLED", False),
            ("app.core.config.settings.CATALOG_KLC_RELEASE_GATE", "warn"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.service = ComponentCatalogPostgresService(
            store_root=Path(self.tempdir.name) / "components",
            database_url=CATALOG_URL,
        )
        self.service.initialize()
        self.addCleanup(self.service.close)
        self.token = uuid.uuid4().hex[:10]

    def _released_part_with_model(self) -> tuple[dict, dict]:
        """A released part whose revision carries one STEP model."""

        created = self.service.create_manual_component(
            value="10k",
            description="STEP purge component",
            datasheet="https://example.com/r.pdf",
            manufacturer="Prism Purge",
            manufacturer_part_number=f"PURGE-{self.token}",
            actor=ACTOR,
        )
        component_id = str(created["id"])
        self.addCleanup(
            self.service.deactivate_component, component_id, actor="purge-test@local", reason="STEP purge test cleanup"
        )
        # Each run writes distinct bytes so asset rows from earlier runs, whose
        # backing files are gone, are never reused.
        symbol = f'''(kicad_symbol_lib (version 20231120) (generator "test")
          (symbol "R_Purge"
            (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (property "Value" "{self.token}" (at 0 0 0) (effects (font (size 1.27 1.27))))
          )
        )'''.encode()
        self.service.import_symbol_library(
            component_id, upload_name="R_Purge.kicad_sym", payload=symbol, target_library="Prism_Purge",
            selected_symbol="R_Purge", actor=ACTOR,
        )
        self.service.import_footprint(
            component_id, upload_name="R_Purge.kicad_mod",
            payload=f'(footprint "R_Purge" (version 20240108) (generator "test") (descr "{self.token}"))'.encode(),
            target_library="Prism_Purge", selected_footprint="R_Purge", actor=ACTOR,
        )
        with_model = self.service.attach_auxiliary_asset(
            component_id, asset_type="3dmodel", upload_name="R_Purge.step",
            payload=f"ISO-10303-21;/* rev1 {self.token} */END-ISO-10303-21;".encode(),
            target_library="Prism_Purge", actor=ACTOR,
        )["component"]
        self.service.set_release_status(component_id, "in_progress", actor=ACTOR)
        self.service.set_release_status(component_id, "qa_review", actor=ACTOR)
        approved = self.service.set_release_status(
            component_id, "done", actor="qa@example.com", actor_role="qa",
            expected_revision_id=with_model["revision_id"], expected_manifest_hash=with_model["manifest_hash"],
        )
        released = self.service.set_release_status(
            component_id, "released", actor=ACTOR, actor_role="designer",
            expected_revision_id=approved["revision_id"], expected_manifest_hash=approved["manifest_hash"],
        )
        self.assertEqual(released["release_status"], "released")
        [model] = [asset for asset in released["assets"] if asset["asset_type"] == "3dmodel"]
        return released, model

    def _replace_model_in_draft(self, released: dict, old_model: dict) -> dict:
        """Draft: attach a new STEP, then detach the released one."""

        component_id = str(released["id"])
        drafted = self.service.attach_auxiliary_asset(
            component_id, asset_type="3dmodel", upload_name="R_Purge.step",
            payload=f"ISO-10303-21;/* rev2 {self.token} */END-ISO-10303-21;".encode(),
            target_library="Prism_Purge", actor=ACTOR,
        )["component"]
        return self.service.detach_asset_by_id(
            component_id, str(old_model["id"]), expected_revision_id=drafted["revision_id"], actor=ACTOR,
        )["component"]

    def _step_path(self, asset_id: str) -> Path:
        with self.service.connection() as conn:
            row = conn.execute("SELECT canonical_path FROM assets WHERE id = %s", (asset_id,)).fetchone()
        return Path(str(row["canonical_path"]))

    def test_draft_with_a_new_model_keeps_the_released_step(self) -> None:
        released, old_model = self._released_part_with_model()
        old_path = self._step_path(str(old_model["id"]))
        self.assertTrue(old_path.is_file())

        draft = self._replace_model_in_draft(released, old_model)
        self.assertNotEqual(draft["revision_id"], released["revision_id"])
        self.assertNotIn(str(old_model["id"]), [str(asset["id"]) for asset in draft["assets"]])
        placed = self.service.get_component(str(released["id"]), released_only=True)
        self.assertEqual(placed["revision_id"], released["revision_id"], "placement still serves the released revision")

        result = self.service.purge_superseded_step_files()

        self.assertNotIn(str(old_model["id"]), result["asset_ids"])
        self.assertTrue(old_path.is_file(), "the released revision's STEP was purged")
        bundle = self.service.build_inline_bundle(str(released["id"]))
        self.assertIsNotNone(bundle)


if __name__ == "__main__":
    unittest.main()
