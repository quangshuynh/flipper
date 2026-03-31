# Flipper - Computer Flipping Intelligence System

A Python-powered tool that scans online listings, extracts computer specs using AI + rules, evaluates deal quality, and sends high-profit opportunities directly to Discord

---

## What This Does

This app helps you find undervalued computers (PCs + laptops) by:
- Scanning listings from marketplaces-
- Extracting specs automatically (even from messy descriptions)
- Detecting “good deal” signals (e.g. *“don’t know much about computers”*)
- Estimating resale value
- Calculating profit potential
- Sending alerts to Discord in real-time

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

## 🛠️ Tech Stack

* Python
* `requests` – fetching data
* `re` – regex parsing
* Optional AI (LLM) – smarter extraction
* `geopy` – distance filtering
* `sqlite` – deduplication
* Discord Webhooks / Bot API

---

## 📈 Scoring Logic (Simplified)

* Profit margin (biggest factor)
* Price vs market value
* Seller knowledge signals
* Distance

