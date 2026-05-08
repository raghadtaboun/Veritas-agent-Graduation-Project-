-- Veritas Agent — PostgreSQL Schema
-- Database: newsguard
-- Apply with: psql -d newsguard -f infra/schema.sql

-- Enable pgvector extension (must be done inside the target database)
CREATE EXTENSION IF NOT EXISTS vector;

-- ─────────────────────────────────────────────
-- Table 1: sources
-- Pre-seeded records of known Arabic news sources with bias labels.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sources (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    domain     TEXT NOT NULL UNIQUE,
    bias_label TEXT NOT NULL CHECK (
        bias_label IN ('pro_government', 'opposition', 'neutral', 'pan_arab', 'western_aligned')
    ),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Table 2: articles
-- Core content store. One row per unique URL.
-- embedding: 768-dim vector from Gemini text-embedding-004
-- entities:  JSONB {people: [], locations: [], organizations: []}
-- published_at: required for Clustering Agent 72-hour time window
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS articles (
    id               SERIAL PRIMARY KEY,
    title            TEXT NOT NULL,
    content          TEXT,
    url              TEXT NOT NULL UNIQUE,
    source_id        INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    section          TEXT NOT NULL CHECK (section IN ('middle_east', 'libya', 'world')),
    published_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    embedding        vector(768),
    entities         JSONB DEFAULT '{}'::jsonb,
    has_full_content BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- HNSW index on embedding for fast approximate nearest-neighbor search
CREATE INDEX IF NOT EXISTS articles_embedding_hnsw_idx
    ON articles USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Index on published_at for efficient time-window filtering in find_similar
CREATE INDEX IF NOT EXISTS articles_published_at_idx
    ON articles (published_at DESC);

-- Index on section for fast section-scoped queries
CREATE INDEX IF NOT EXISTS articles_section_idx
    ON articles (section);

-- ─────────────────────────────────────────────
-- Table 3: events
-- Event clusters discovered by the Clustering Agent.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id              SERIAL PRIMARY KEY,
    section         TEXT NOT NULL CHECK (section IN ('middle_east', 'libya', 'world')),
    headline        TEXT,
    summary         TEXT,
    bias_assessment TEXT,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Table 4: article_events
-- Many-to-many join: links articles to events with a relevance score.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS article_events (
    id              SERIAL PRIMARY KEY,
    article_id      INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    relevance_score FLOAT NOT NULL DEFAULT 1.0,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (article_id, event_id)
);

CREATE INDEX IF NOT EXISTS article_events_event_id_idx
    ON article_events (event_id);

CREATE INDEX IF NOT EXISTS article_events_article_id_idx
    ON article_events (article_id);

-- ─────────────────────────────────────────────
-- Table 5: bias_scores
-- Political score, label, confidence, and framing per article.
-- score: -1.0 (strongly opposition) to +1.0 (strongly pro-government)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bias_scores (
    id         SERIAL PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE UNIQUE,
    score      FLOAT NOT NULL CHECK (score >= -1.0 AND score <= 1.0),
    label      TEXT NOT NULL CHECK (
        label IN ('pro_government', 'opposition', 'neutral', 'pan_arab', 'western_aligned')
    ),
    confidence FLOAT NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    framing    TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS bias_scores_label_idx
    ON bias_scores (label);

-- ─────────────────────────────────────────────
-- Table 6: blindspot_reports
-- Coverage distribution and missing-perspective analysis per event.
-- coverage_stats: JSONB {pro_government: N, opposition: N, neutral: N, ...}
-- missing_perspectives: array of underrepresented bias labels
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS blindspot_reports (
    id                   SERIAL PRIMARY KEY,
    event_id             INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    coverage_stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_perspectives TEXT[] NOT NULL DEFAULT '{}',
    created_at           TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS blindspot_reports_event_id_idx
    ON blindspot_reports (event_id);

-- ─────────────────────────────────────────────
-- Seed Data: Known Arabic news sources with pre-labeled bias
-- ─────────────────────────────────────────────
INSERT INTO sources (name, domain, bias_label) VALUES
    ('العربية',           'alarabiya.net',        'pro_government'),
    ('سكاي نيوز عربية',  'skynewsarabia.com',    'pro_government'),
    ('عربي21',            'arabi21.com',          'opposition'),
    ('ميدل إيست آي',      'middleeasteye.net',    'opposition'),
    ('بي بي سي عربي',    'bbc.com',              'neutral'),
    ('دويتشه فيله عربي', 'dw.com',               'neutral'),
    ('الجزيرة',           'aljazeera.net',        'pan_arab'),
    ('الميادين',          'mayadeen.com',         'pan_arab'),
    ('فرانس 24 عربي',    'france24.com',         'western_aligned'),
    ('RT عربي',           'arabic.rt.com',        'western_aligned'),
    ('ليبيا المستقبل',   'libyaalmostakbal.com', 'opposition'),
    ('بوابة الوسط',      'alwasat.ly',           'neutral'),
    ('قناة ليبيا',        'libyatv.ly',           'pro_government')
ON CONFLICT (domain) DO NOTHING;
