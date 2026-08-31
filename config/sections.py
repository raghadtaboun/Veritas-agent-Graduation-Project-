# config/sections.py — Veritas Agent Section Configuration
#
# This file contains ONLY configuration data. No functions, no imports,
# no executable logic. All three sections must remain in this single file.
# update_every_hours: recommended refresh cadence per section (informational; pipeline is triggered manually).

SECTIONS: dict = {
    "middle_east": {
        "label": "أخبار الشرق الأوسط",
        "query_ar": "فلسطين",
        "query_en": "Palestine",
        # Concise query used directly in GDELT API calls (GDELT is slow with long OR chains)
        "query_gdelt": "Palestine",
        "countries": [],
        "sources": [
            "aljazeera.net",
            "alarabiya.net",
            "skynewsarabia.com",
            "arabi21.com",
            "middleeasteye.net",
            "bbc.com",
            "dw.com",
            "france24.com",
            "arabic.rt.com",
            "mayadeen.com",
        ],
        "update_every_hours": 6,
        "max_articles_per_run": 70,
    },

    "libya": {
        "label": "أخبار ليبيا",
        "query_ar": "ليبيا",
        "query_en": "Libya",
        "query_gdelt": "Libya",
        "countries": ["LY"],
        "sources": [
            "aljazeera.net",
            "alarabiya.net",
            "libyaalmostakbal.com",
            "alwasat.ly",
            "libyatv.ly",
            "bbc.com",
            "france24.com",
            "middleeasteye.net",
        ],
        "update_every_hours": 4,
        "max_articles_per_run": 100,
    },

    "world": {
        "label": "أخبار العالم",
        "query_ar": "الولايات المتحدة",
        "query_en": "United States",
        "query_gdelt": "United States",
        "countries": [],
        "sources": [
            "aljazeera.net",
            "alarabiya.net",
            "bbc.com",
            "dw.com",
            "france24.com",
            "arabic.rt.com",
            "skynewsarabia.com",
        ],
        "update_every_hours": 6,
        "max_articles_per_run": 100,
    },
}
