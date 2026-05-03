# FFXIV Craft Planner

A local web app for planning Grand Company supply & provision quests in Final Fantasy XIV.

## Requirements

- Python 3.10 or newer — https://python.org
- Internet connection (uses XIVAPI for live recipe data)

## Quick Start

### Windows
Double-click `launch.bat`

That's it. The script will:
1. Check that Python is installed
2. Auto-install Flask and Requests if missing
3. Open your browser to http://localhost:5000

### Mac / Linux
```bash
chmod +x launch.sh
./launch.sh
```

## How to Use

1. **Search** — Type an item name in the search box and hit Search. Use the quantity field to set how many you need.

2. **Select** — Click the item from the results list.

3. **Mark what you have** — Click "Calculate Breakdown" first to see the ingredient tree.
   On any leaf ingredient (raw material), click **"Have Some?"** to add it to the "Items I Already Have" panel.
   Enter how many you already have — those will be subtracted from the required totals.

4. **Recalculate** — Hit "Calculate Breakdown" again. Any component you have enough of will be crossed out in the tree.

5. **Gathering List** — Scroll down to see your material list:
   - **By Source** tab: split into Gathered / Crystals / Mob Drops / Other
   - **All Totals** tab: flat sorted list of everything
   - Click any item in the gathering list to tick it off as you collect it

## Tips

- For **provision quests** (gather X of item Y), just search the raw material directly and set the quantity.
- For **supply quests** (craft X), search the finished item. The tree will recursively break down every sub-component.
- The "Have Some?" button only appears on raw/leaf materials. For intermediate crafted items, add their sub-materials instead.
- The gathering list persists your tick-marks while the page is open — recalculating will refresh it.

## Notes

- Recipe data comes from [XIVAPI](https://xivapi.com) — free, no API key needed.
- Source classification (gathered vs. mob drop) is based on item category from XIVAPI and is a best-effort guess. Some items may be miscategorised.
- The app runs entirely locally. No data is sent anywhere except XIVAPI for recipe lookups.
