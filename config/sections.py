# config/sections.py — Veritas Agent Section Configuration
#
# This file contains ONLY configuration data. No functions, no imports,
# no executable logic. All three sections must remain in this single file.
# The scheduler reads update_every_hours independently per section.

SECTIONS: dict = {
    "middle_east": {
        "label": "أخبار الشرق الأوسط",
        "query_ar": " فلسطين OR إسرائيل ",
        "query_en": "Palestine OR Israel",
        # Concise query used directly in GDELT API calls (GDELT is slow with long OR chains)
        "query_gdelt": "Palestine Israel",
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
        "max_articles_per_run": 100,
    },

    "libya": {
        "label": "أخبار ليبيا",
        "query_ar": "ليبيا OR طرابلس OR بنغازي OR الدبيبة OR حفتر OR المنفي OR الوحدة الوطنية",
        "query_en": "Libya OR Tripoli OR Benghazi OR Dbeibah OR Haftar OR National Unity",
        "query_gdelt": "Libya Tripoli Benghazi",
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
        "query_ar": "أمريكا",
        "query_en": "United States",
        "query_gdelt": "United States ",
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
