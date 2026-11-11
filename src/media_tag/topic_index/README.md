# Topic draft index

Projects current qualified precision fields into the existing draft projection
slot. It reuses precision.projection.verified; caches can reject nonmatches but
never authorize returned values. Pending/failed/cancelled newer scopes mask old
field successes. Human effective values and explicit rejections take priority.
Public projections contain evidence hashes, never private artifact paths.
