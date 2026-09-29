# Q-numbers and lifecycle

A Q-number is Flipper's durable human-facing inventory identity (`Q0001`, `Q0002`, and so on). The
allocator is transactional and numbers are never reused, including after correction or archival.
Marketplace SKUs may link to Q-numbers, but external linkage does not replace local identity.

Items begin **acquired**, may become **listed**, then **sold** or **archived**. Supported correction
paths can return sold to listed and listed to acquired. Timestamps record known lifecycle facts;
Flipper does not invent historical transitions. Archive has no timestamp today, which limits aging
and historical reporting.
