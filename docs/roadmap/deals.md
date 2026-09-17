# Generalized Deals direction

The source-neutral deal model and ephemeral economics/comparison core are implemented. A first-class
Deals web workspace and vendor ingestion are still planned; the application does not expose a dead
Deals route. The current PC analyzer remains supported as an Electronics specialization.

## Candidate categories

Vehicle Parts; Electronics; Collectibles & Art; Home & Garden; Clothing, Shoes & Accessories; Toys
& Hobbies; Sporting Goods; Books, Movies & Music; Health & Beauty; Business & Industrial; Jewelry
& Watches; Baby Essentials; Pet Supplies; Tickets & Travel; Everything Else; Real Estate; Gift Cards
& Coupons; and Specialty Services.

## Sources and adapters

Candidate boundaries include eBay, Facebook Marketplace, Mercari, Craigslist, estate-sale sources,
local opportunities, and other permitted feeds/vendors. A name here is not a promise of automated
support. Adapters must use suitable official APIs, permitted feeds/exports, or user-provided data;
brittle or prohibited crawling is not the architecture.

## Economics foundation

The core distinguishes base purchase price, estimated tax, inbound shipping,
pickup/travel cost, selling fees, outbound shipping, expected resale proceeds, expected net profit,
ROI, capital tied up, market liquidity, estimated time-to-sale, sell-through evidence, profit
velocity, condition uncertainty, comparable-sale quality, confidence, and downside risk.

A deal expected to make approximately $80 but require weeks to sell is not automatically superior
to one expected to make approximately $40 that can reliably turn over in days. Comparison preserves
those tradeoffs; no global weighted score is used.

## Local opportunities

Future design may consider a configured search region, distance, pickup time, acquisition travel
cost, ability to acquire/list quickly, and local estate sales. This interval adds no precise-location
request, location tracking, maps, or geocoding.
