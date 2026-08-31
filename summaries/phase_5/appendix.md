# Phase 5 Appendix: UI/UX Newspaper Theme Refactoring

> **Purpose:** This appendix documents the visual and user experience (UX) enhancements applied to the Streamlit dashboard after the initial Phase 5 implementation. The goal was to transform the default Streamlit aesthetic into a clean, classic "newspaper" or news-site layout, without altering the underlying architecture, API contracts, or routing logic.

---

## 1. Global Theme and Typography
- **Streamlit Config (`.streamlit/config.toml`)**: Added a custom `[theme]` configuration to enforce a light, newspaper-inspired palette. This includes an off-white paper background (`#FBFBF9`), near-black ink text (`#111111`), and the project's primary blue (`#1E5BFF`) as the accent color.
- **Custom Web Fonts (`frontend/styles.py`)**: Imported a mix of serif and sans-serif fonts to elevate the typography:
  - `Playfair Display` for the main Latin masthead.
  - `Amiri` and `Noto Naskh Arabic` for elegant Arabic serif headlines.
  - `Source Sans 3` for readable body text.

## 2. Masthead and Top Navigation
- **Relocated Navigation (`frontend/app.py`)**: Moved the primary navigation radio buttons out of the sidebar and into the top of the main content column (`_render_nav`). 
- **Centered Layout**: The masthead ("Veritas Agent" and its subtitle) was centered and separated from the content by a classic newspaper double-rule line. The horizontal navigation bar was strictly centered using CSS Flexbox (`display: flex; flex-direction: column; align-items: center;`) to reliably handle RTL layout constraints.
- **Sidebar Collapsed**: Set `initial_sidebar_state="collapsed"` and hid the sidebar toggle button via CSS to enforce the top-nav-only experience.
- **Label Cleanup**: Removed emojis from the Arabic navigation labels (e.g., "🏠 الرئيسية" became "الرئيسية") for a more professional, editorial look.

## 3. Page-Specific Cleanups
- **Home Page (`frontend/pages/home.py`)**: 
  - Removed the redundant "Sections" (الأقسام) button cards from the top of the page, as the new top navigation makes them unnecessary.
  - Removed the `(ⓘ)` tooltip icons next to the "Latest News" and "Latest Events" headers to reduce visual clutter.
  - **Technical Fix (Callback Navigation)**: Resolved a `StreamlitAPIException` when clicking the "عرض كل الأحداث ←" (See all events) button by using a Streamlit callback (`on_click=_go_to_events`). This ensures the session state updates safely before the next render cycle begins.
- **All Events Page (`frontend/pages/all_events.py`)**:
  - Removed the `(ⓘ)` tooltip icon from the main page header.
- **Article Links (`frontend/components.py`)**: Styled article titles as large, serif headlines (`.veritas-headline`) that underline on hover, mimicking traditional news links.

## 4. New "About" Page
- **Created `frontend/pages/about.py`**: Added a dedicated, non-technical informational page ("عن النظام") to explain the system to end-users.
- **Content**:
  - A brief explanation of what Veritas Agent does (aggregating and framing analysis).
  - A clear breakdown of the 5 Arabic-context bias labels (`pro_government`, `opposition`, `pan_arab`, `western_aligned`, `neutral`).
  - An explanation of the "Confidence Score" (مستوى الثقة), clarifying that the number (0.0 to 1.0) represents the AI's certainty in its classification, not the extremity of the article's bias.

## 5. Global Footer and State Management
- **Footer Implementation (`frontend/app.py`)**: Added a `_render_footer()` function called at the end of the main script to display at the bottom of every page.
- **Contents**: Includes a subtle top border, a centered button linking to the "About" page, and a LTR-aligned copyright notice: `© 2026 veritas agent by Raghad Muftah Taboun`.
- **Technical Fix (Callback Navigation)**: Resolved a `StreamlitAPIException` (`st.session_state.nav cannot be modified after the widget...`) by using a Streamlit callback (`on_click=_go_to_about`) for the footer button. This ensures the session state updates safely before the next render cycle begins, rather than attempting to mutate state during the current top-down execution.