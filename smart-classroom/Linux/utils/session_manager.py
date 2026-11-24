import re
import uuid
from datetime import datetime

# Session ids that end up in a filesystem path must be a single, relative path
# segment: no separator, no drive letter, and no leading dot so "." and ".."
# cannot slip through. Same pattern as the Windows variant.
PATH_SAFE_SESSION_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_PATH_SAFE_RE = re.compile(PATH_SAFE_SESSION_ID)


def generate_session_id():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    short_uid = str(uuid.uuid4())[:4]  # short random suffix
    return f"{timestamp}-{short_uid}"


def is_path_safe_session_id(session_id) -> bool:
    """True if `session_id` is safe to use as a directory name."""
    return isinstance(session_id, str) and bool(_PATH_SAFE_RE.match(session_id))
