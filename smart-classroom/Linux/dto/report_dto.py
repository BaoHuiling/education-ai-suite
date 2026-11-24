from pydantic import BaseModel, Field
from typing import Optional, List

from utils.session_manager import PATH_SAFE_SESSION_ID


class ReportRequest(BaseModel):
    # Validated here because Linux SessionPaths does not vet session ids and
    # this one becomes a directory name.
    session_id: str = Field(pattern=PATH_SAFE_SESSION_ID)
    query: Optional[str] = None
    # Field codes the teacher checked in the UI. None => include the whole
    # catalog (full report); deselected fields are dropped from the report.
    selected_fields: Optional[List[str]] = None
    # Teacher-typed values for manual fields (school/class/course/teacher).
    # {field_code: value}; blank values are treated as deselected.
    manual_fields: Optional[dict] = None


class ReportReselectRequest(BaseModel):
    # New checkbox selection to re-project onto the cached report — no LLM.
    selected_fields: Optional[List[str]] = None
    # Updated manual field values (basic info) to apply during re-projection.
    manual_fields: Optional[dict] = None