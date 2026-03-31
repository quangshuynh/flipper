# Flipper - Computer Flipping Intelligence System

A Python-powered tool that scans online listings, extracts computer specs using AI + rules, evaluates deal quality, and sends high-profit opportunities directly to Discord

---

## Why This Matters

Finding profitable computer deals manually is time-consuming and inconsistent. Listings are often messy, incomplete, or undervalued due to seller inexperience

Flipper automates this process by:
- Interpreting unstructured listings
- Identifying undervalued systems
- Estimating resale value
- Surfacing only the best opportunities

---

## What This Does

Flipper helps identify undervalued PCs and laptops by:

- Scanning listings from supported sources (modular collectors)
- Extracting hardware specs from messy descriptions
- Detecting seller signals that indicate undervaluation
- Estimating resale value using heuristic pricing models
- Calculating profit margins
- Sending real-time alerts to Discord
  
---

## Core Features

### Spec Extraction (Smart Parsing)

Automatically pulls:
* GPU
* CPU
* RAM
* Storage
* PSU → `"not listed"` if missing
* Motherboard → `"not listed"`
* Case → `"not listed"`
* CPU Cooler → `"not listed"`
* OS → `"not listed"`

### Extras Detection

Identifies included items:

* Monitor
* Keyboard
* Mouse
* Other accessories

---

### Smart Deal Signals

Detects phrases that often indicate undervalued listings:

* “don’t know much about computers”
* “just want gone”
* “moving sale”
* “not sure what it has”

---

### Pricing Intelligence

For every listing:

* Listing price
* Estimated resale value
* Ideal buy price
* Ideal sell price
* Estimated profit

---

### Location Filtering

* Filters listings within a ~100 mile radius of Rochester
* Designed for local flipping markets (e.g. Rochester / college areas)

---

### Discord Alerts

Sends clean, structured deal alerts:

```
🚨 POTENTIAL DEAL FOUND

💻 Specs
GPU: RTX 3060
CPU: Ryzen 5
RAM: 16GB
Storage: not listed
...

📦 Extras
Monitor, Keyboard

🚩 Flags
Low knowledge seller

💰 Pricing
Listing: $400
Ideal Buy: $260
Sell: $550
Profit: $150

🔗 Link to listing

---

## How It Works

1. Fetch listings
2. Parse text → extract specs
3. Detect deal signals
4. Estimate value
5. Score deal
6. Filter by distance
7. Send best deals to Discord

---

## Example Flow

Input listing:

> “Gaming PC, RTX 3060, 16gb ram, Ryzen 5, don’t know much about computers, comes with monitor”

Output:

```json
{
  "gpu": "RTX 3060",
  "cpu": "Ryzen 5",
  "ram": "16GB",
  "storage": "not listed",
  "extras": ["monitor"],
  "flags": ["low_knowledge_seller"]
}
```

---

## Tech Stack

* Python
* `requests` – fetching data
* `re` – regex parsing
* Optional AI (LLM) – smarter extraction
* `geopy` – distance filtering
* `sqlite` – deduplication
* Discord Webhooks / Bot API

---

## Scoring Logic (Simplified)

* Profit margin (biggest factor)
* Price vs market value
* Seller knowledge signals
* Distance

---

## Limitations

* Listings may omit key specs
* Pricing estimates are heuristic-based
* Some platforms restrict automated data access
* Condition (e.g. battery health, damage) is not always detectable

---

## Future Improvements
- Image-based component detection (GPU, case, etc.)
- Historical price tracking
- Auto-messaging sellers
- Machine learning pricing model
- Mobile dashboard
