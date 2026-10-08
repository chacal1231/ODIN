from ..db import session
from .service import origin_view
with session() as db:
    if origin_view(db)['state']!='healthy':raise SystemExit(1)
