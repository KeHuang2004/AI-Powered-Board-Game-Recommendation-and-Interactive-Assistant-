PRAGMA user_version = 2;

CREATE TABLE games (
    game_id TEXT PRIMARY KEY NOT NULL,
    requires_host INTEGER NOT NULL CHECK (requires_host IN (0, 1)),
    requires_dedicated_components INTEGER NOT NULL CHECK (requires_dedicated_components IN (0, 1)),
    tier TEXT NOT NULL CHECK (tier IN ('T0', 'T1', 'T2'))
);

CREATE TABLE game_text (
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    locale TEXT NOT NULL CHECK (locale IN ('en', 'zh')),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    summary TEXT NOT NULL,
    core_rules TEXT NOT NULL,
    detailed_rules TEXT NOT NULL,
    catalog_reason TEXT NOT NULL,
    PRIMARY KEY (game_id, locale),
    UNIQUE (locale, name)
);

CREATE TABLE game_player_counts (
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    players INTEGER NOT NULL CHECK (players BETWEEN 1 AND 100),
    recommended INTEGER NOT NULL CHECK (recommended IN (0, 1)),
    PRIMARY KEY (game_id, players)
);

CREATE INDEX game_player_counts_by_players ON game_player_counts(players, game_id);

CREATE TABLE game_tags (
    game_id TEXT NOT NULL,
    locale TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    tag TEXT NOT NULL CHECK (length(trim(tag)) > 0),
    PRIMARY KEY (game_id, locale, position),
    UNIQUE (game_id, locale, tag),
    FOREIGN KEY (game_id, locale) REFERENCES game_text(game_id, locale) ON DELETE CASCADE
);

CREATE TABLE game_extra (
    game_id TEXT NOT NULL,
    locale TEXT NOT NULL CHECK (locale IN ('en', 'zh')),
    setup_items TEXT NOT NULL,
    conditions TEXT NOT NULL,
    win_relation TEXT NOT NULL,
    interaction TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    aliases TEXT NOT NULL,
    translated INTEGER NOT NULL CHECK (translated IN (0, 1)),
    PRIMARY KEY (game_id, locale),
    FOREIGN KEY (game_id, locale) REFERENCES game_text(game_id, locale) ON DELETE CASCADE
);

CREATE TABLE rulebooks (
    game_id TEXT PRIMARY KEY NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    text TEXT NOT NULL,
    est_tokens INTEGER NOT NULL
);
