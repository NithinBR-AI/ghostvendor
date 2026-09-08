"""
Patch schema — input contract and output artifact for Agent 6 (Patch Generator).

PatchRequest is what Agent 6 receives from the artifact store.
PatchResult is what Agent 6 produces — one per vendor, collected into PatchReport.
"""

from pydantic import BaseModel
from models.diagnosis import DiagnosisResult, FixStrategy


class PatchResult(BaseModel):
    vendor: str
    affected_file: str
    fix_strategy: FixStrategy
    patch_diff: str          # unified diff format — applied to the repo
    pr_title: str
    pr_description: str      # markdown — explains what broke and why this fix works
    sandbox_validated: bool = False  # set to True after Contree confirms the patch passes


class PatchReport(BaseModel):
    repository: str
    patches: list[PatchResult]
    pr_url: str = ""

    def for_vendor(self, vendor_name: str) -> PatchResult | None:
        for p in self.patches:
            if p.vendor == vendor_name:
                return p
        return None
