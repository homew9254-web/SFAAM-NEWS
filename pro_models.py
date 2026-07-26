"""
pro_models.py — SFAAM NEWS PRO 1 — Additional database models
============================================================

Adds Pro-tier tables on top of the V32.1 schema. All tables are
prefixed with `pro_` to avoid collisions and to make migrations
easy to identify.

Tables added:
  - ProAuthor           — real author profiles (replaces "Editorial Team")
  - ProTopic            — Wikipedia-style topic aggregation pages
  - ProArticleTopic     — many-to-many between articles and topics
  - ProReadingHistory   — per-reader reading history (anonymous fingerprint)
  - ProBookmarkFolder   — user-named bookmark collections
  - ProBookmarkItem     — article inside a folder
  - ProHighlight        — Medium-style text highlights
  - ProReaction         — emoji-style reactions (like, love, insightful)
  - ProCommentThread    — threaded comments (parent_id)
  - ProPushSubscription — Web Push VAPID subscriptions
  - ProDigestSubscriber — daily/weekly email digest subscribers
  - ProCorrection       — per-article correction log
  - ProCitation         — inline citation references for articles
  - ProABTest           — A/B test assignments (headline/layout)

Design principles:
  - All tables use BIGINT auto-increment PKs.
  - All tables have created_at + updated_at timestamps.
  - All "user" tables key off an anonymous fingerprint string
    (no PII required to use the personalized features; email only
    for digests). This keeps us GDPR-friendly out of the box.
  - Indexes on every foreign-key column and every column used in
    WHERE / ORDER BY clauses.
"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Integer,
    String, Text, UniqueConstraint, Index, JSON, Column,
)
from sqlalchemy.orm import relationship

# Import the Base from the existing database module so all models
# share the same metadata and Alembic can auto-detect them.
from database import Base


# ─────────────────────────────────────────────────────────────
# 1. AUTHORS — Real bylines instead of "Editorial Team"
# ─────────────────────────────────────────────────────────────
class ProAuthor(Base):
    """A real author profile. Replaces the generic 'Editorial Team' byline.

    Stored separately from `articles.author` (which is just a string) so
    that an author can have many articles, a bio, an avatar, expertise
    tags, and a credibility score that grows over time.
    """
    __tablename__ = "pro_authors"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    slug          = Column(String(120), nullable=False, unique=True, index=True)
    name          = Column(String(120), nullable=False)
    title         = Column(String(200), nullable=True)  # e.g. "Senior Political Correspondent"
    bio           = Column(Text, nullable=True)
    avatar_url    = Column(String(1000), nullable=True)
    twitter       = Column(String(100), nullable=True)
    linkedin      = Column(String(200), nullable=True)
    email         = Column(String(200), nullable=True)  # for contact
    expertise     = Column(JSON, nullable=True)  # ["politics","economics","middle-east"]
    is_active     = Column(Boolean, default=True)
    credibility   = Column(Float, default=50.0)  # 0-100, grows with verified articles
    articles_count = Column(Integer, default=0)  # denormalized for fast sort
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 2. TOPICS — Wikipedia-style aggregation pages
# ─────────────────────────────────────────────────────────────
class ProTopic(Base):
    """A long-running topic / story arc that aggregates multiple articles.

    Examples:
      - "US-China Trade War"
      - "2024 Pakistan Elections"
      - "Climate Change Policy"

    Topic pages are SEO pillar pages: they summarize the story, link to
    every article in the topic, and provide a timeline. This is what
    Wikipedia does for ongoing news events.
    """
    __tablename__ = "pro_topics"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    slug          = Column(String(200), nullable=False, unique=True, index=True)
    title         = Column(String(300), nullable=False)
    summary       = Column(Text, nullable=True)
    # Long-form description (markdown). Updated as the story develops.
    description   = Column(Text, nullable=True)
    # Cover image for the topic page + social shares
    image_url     = Column(String(1000), nullable=True)
    # Region ("world", "us", "pk"...) for routing
    region        = Column(String(50), nullable=True, index=True)
    # Category ("politics", "economy", "sports"...)
    category      = Column(String(50), nullable=True, index=True)
    # Tags for related-topic discovery
    tags          = Column(JSON, nullable=True)
    # Status: "active" (story ongoing) | "archived" (story ended)
    status        = Column(String(20), default="active", index=True)
    # Denormalized counts for fast display
    articles_count = Column(Integer, default=0)
    followers_count = Column(Integer, default=0)
    # SEO
    meta_desc     = Column(String(300), nullable=True)
    # Timestamps
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProArticleTopic(Base):
    """Many-to-many: which articles belong to which topics."""
    __tablename__ = "pro_article_topics"
    __table_args__ = (
        UniqueConstraint("article_id", "topic_id", name="uq_pro_arttopic"),
        Index("ix_pro_arttopic_topic", "topic_id", "article_id"),
    )

    id         = Column(BigInteger, primary_key=True, autoincrement=True)
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True)
    topic_id   = Column(BigInteger, ForeignKey("pro_topics.id", ondelete="CASCADE"), nullable=False, index=True)
    relevance  = Column(Float, default=1.0)  # for sorting within topic page
    added_at   = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 3. READING HISTORY — Personalization
# ─────────────────────────────────────────────────────────────
class ProReadingHistory(Base):
    """Tracks what each anonymous reader has read, for personalization.

    Keyed off a fingerprint (same one used for likes/comments). No PII.
    Used to build the 'For You' feed on the homepage.
    """
    __tablename__ = "pro_reading_history"
    __table_args__ = (
        UniqueConstraint("fingerprint", "article_id", name="uq_pro_rh"),
        Index("ix_pro_rh_fp_date", "fingerprint", "read_at"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint  = Column(String(100), nullable=False, index=True)
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    region       = Column(String(50), nullable=True)  # denormalized for fast interest graph
    read_at      = Column(DateTime, default=datetime.utcnow, index=True)
    read_pct     = Column(Float, default=0.0)  # how far they scrolled (0-1)
    time_on_page = Column(Integer, default=0)  # seconds
    # User feedback signal (1 = thumbs up, -1 = thumbs down, 0 = none)
    feedback     = Column(Integer, default=0)


# ─────────────────────────────────────────────────────────────
# 4. BOOKMARK FOLDERS — Organize saved articles
# ─────────────────────────────────────────────────────────────
class ProBookmarkFolder(Base):
    """A user-named folder for organizing bookmarks (e.g. 'Read later',
    'Pakistan politics', 'Climate research')."""
    __tablename__ = "pro_bookmark_folders"
    __table_args__ = (
        UniqueConstraint("fingerprint", "name", name="uq_pro_bmf"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint  = Column(String(100), nullable=False, index=True)
    name         = Column(String(100), nullable=False)
    description  = Column(String(300), nullable=True)
    is_public    = Column(Boolean, default=False)  # shareable folder URL
    created_at   = Column(DateTime, default=datetime.utcnow)


class ProBookmarkItem(Base):
    """An article saved into a folder."""
    __tablename__ = "pro_bookmark_items"
    __table_args__ = (
        UniqueConstraint("folder_id", "article_id", name="uq_pro_bmi"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    folder_id    = Column(BigInteger, ForeignKey("pro_bookmark_folders.id", ondelete="CASCADE"), nullable=False, index=True)
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    added_at     = Column(DateTime, default=datetime.utcnow)
    notes        = Column(Text, nullable=True)  # user's note for this bookmark


# ─────────────────────────────────────────────────────────────
# 5. HIGHLIGHTS — Medium-style text selection + save
# ─────────────────────────────────────────────────────────────
class ProHighlight(Base):
    """A highlighted text snippet from an article, with optional note.

    Like Medium's highlight feature. Public highlights are aggregated
    per article so readers can see what others found important.
    """
    __tablename__ = "pro_highlights"
    __table_args__ = (
        Index("ix_pro_hl_article", "article_id", "created_at"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True)
    fingerprint  = Column(String(100), nullable=False, index=True)
    # The exact text highlighted (we store text not offsets — robust to DOM changes)
    highlighted_text = Column(Text, nullable=False)
    # Optional note the user added
    note         = Column(Text, nullable=True)
    # Color: yellow (default), green, blue, pink
    color        = Column(String(20), default="yellow")
    is_public    = Column(Boolean, default=True)
    # Count of "agree" reactions from other users
    agrees_count = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 6. REACTIONS — Emoji-style reactions (not just likes)
# ─────────────────────────────────────────────────────────────
class ProReaction(Base):
    """A reader's reaction to an article. Replaces the binary like with
    a richer signal set: like, love, insightful, celebrate, disagree.

    The 'disagree' reaction is valuable feedback — it tells us the
    article might be controversial or factually disputed, and lets us
    surface alternate perspectives.
    """
    __tablename__ = "pro_reactions"
    __table_args__ = (
        UniqueConstraint("fingerprint", "article_id", name="uq_pro_rx"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint  = Column(String(100), nullable=False, index=True)
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True)
    # One of: like, love, insightful, celebrate, disagree
    reaction     = Column(String(20), nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 7. THREADED COMMENTS
# ─────────────────────────────────────────────────────────────
class ProCommentThread(Base):
    """Threaded comment system replacing the flat ArticleComment table.

    Supports parent_id for replies, upvotes for sorting, and an
    is_approved flag for moderation queue. Comments with links or
    flagged words go to the moderation queue before going live.
    """
    __tablename__ = "pro_comments"
    __table_args__ = (
        Index("ix_pro_c_article", "article_id", "is_approved", "created_at"),
        Index("ix_pro_c_parent", "parent_id"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_id    = Column(BigInteger, ForeignKey("pro_comments.id", ondelete="CASCADE"), nullable=True)
    fingerprint  = Column(String(100), nullable=False, index=True)
    author_name  = Column(String(80), nullable=True)
    body         = Column(Text, nullable=False)
    # Moderation: pending → approved | rejected | flagged
    is_approved  = Column(Boolean, default=True, index=True)
    moderation_note = Column(String(300), nullable=True)
    upvotes      = Column(Integer, default=0)
    downvotes    = Column(Integer, default=0)
    # AI spam score 0-1 (set by moderation pipeline)
    spam_score   = Column(Float, default=0.0)
    created_at   = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 8. PUSH NOTIFICATIONS — VAPID-based web push
# ─────────────────────────────────────────────────────────────
class ProPushSubscription(Base):
    """A Web Push subscription. Used to send breaking-news push
    notifications to users who opted in (mobile + desktop Chrome/
    Firefox/Edge; Safari uses Apple Push Notification Service
    separately, not covered here).

    Subscriptions are keyed off the anonymous fingerprint + the
    push endpoint URL (which is unique per browser).
    """
    __tablename__ = "pro_push_subscriptions"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_pro_ps_endpoint"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint  = Column(String(100), nullable=False, index=True)
    endpoint     = Column(String(1000), nullable=False)
    p256dh       = Column(String(300), nullable=False)
    auth         = Column(String(200), nullable=False)
    # User's preferred notification categories (politics, sports, ...)
    topics       = Column(JSON, nullable=True)
    # Region the user cares about (for region-targeted breaking news)
    region       = Column(String(50), nullable=True)
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 9. EMAIL DIGESTS — Daily / weekly personalized newsletters
# ─────────────────────────────────────────────────────────────
class ProDigestSubscriber(Base):
    """A subscriber to the personalized email digest.

    Distinct from the legacy `Subscriber` table (which is the
    one-off newsletter signup). Digest subscribers get a daily or
    weekly email with the top 5-10 articles in their chosen regions
    and topics.
    """
    __tablename__ = "pro_digest_subscribers"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    email         = Column(String(200), nullable=False, unique=True, index=True)
    # One of: daily, weekly, breaking-only
    frequency     = Column(String(20), default="daily", index=True)
    # JSON array of regions the subscriber cares about
    regions       = Column(JSON, nullable=True)
    # JSON array of topic slugs the subscriber follows
    topics        = Column(JSON, nullable=True)
    # Double opt-in: pending → confirmed | unsubscribed
    status        = Column(String(20), default="pending", index=True)
    confirm_token = Column(String(100), nullable=True)
    unsub_token   = Column(String(100), nullable=True, unique=True)
    last_sent_at  = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 10. CORRECTIONS — Per-article correction log
# ─────────────────────────────────────────────────────────────
class ProCorrection(Base):
    """A published correction for an article. Visible on the article
    page (so readers see what was changed and why) and aggregated on
    /corrections.html.

    This is critical for trust. Wikipedia's editorial credibility
    rests partly on its transparency about edits; we mirror that.
    """
    __tablename__ = "pro_corrections"
    __table_args__ = (
        Index("ix_pro_corr_article", "article_id", "corrected_at"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True)
    # One of: factual_error, typo, clarification, updated_info, retraction
    correction_type = Column(String(30), nullable=False)
    # The original text that was wrong
    original_text = Column(Text, nullable=True)
    # The corrected text
    corrected_text = Column(Text, nullable=True)
    # Editor's note explaining the correction
    editor_note  = Column(Text, nullable=False)
    corrected_by = Column(String(120), nullable=True)
    corrected_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─────────────────────────────────────────────────────────────
# 11. CITATIONS — Inline [1][2] reference system
# ─────────────────────────────────────────────────────────────
class ProCitation(Base):
    """An inline citation attached to an article. Renders as a
    superscript [1] that, on hover/click, shows a card with the
    source's domain, headline, URL, and quote.

    This is the Wikipedia-grade citation system. Every factual claim
    should have at least one citation.
    """
    __tablename__ = "pro_citations"
    __table_args__ = (
        Index("ix_pro_cit_article", "article_id", "position"),
    )

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True)
    position     = Column(Integer, nullable=False)  # [1], [2], [3]...
    source_domain = Column(String(200), nullable=True)
    source_url   = Column(String(1000), nullable=False)
    source_title = Column(String(500), nullable=True)
    # The exact quote from the source that supports the claim
    quoted_text  = Column(Text, nullable=True)
    # When the source was published (for recency check)
    source_date  = Column(DateTime, nullable=True)
    # Authoritative domain? (Reuter, BBC, AP, etc.)
    is_authoritative = Column(Boolean, default=False)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# 12. A/B TESTS — Headline / layout experiments
# ─────────────────────────────────────────────────────────────
class ProABTest(Base):
    """A/B test assignment: which variant of a headline or layout
    each visitor sees. Used to optimize CTR and time-on-page."""
    __tablename__ = "pro_ab_tests"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    test_name    = Column(String(100), nullable=False, index=True)  # e.g. "headline_v1_vs_v2"
    article_id   = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"), nullable=True)
    fingerprint  = Column(String(100), nullable=False, index=True)
    variant      = Column(String(20), nullable=False)  # "A" | "B" | "C"
    # Track outcome: clicked | read | bounced | shared
    outcome      = Column(String(20), nullable=True)
    assigned_at  = Column(DateTime, default=datetime.utcnow)
    resolved_at  = Column(DateTime, nullable=True)


# Late import to avoid circular: Column is exported by database
# (Column is already imported above from sqlalchemy; this is kept for
# backward compatibility with older code that expected the late import.)


async def create_pro_tables(engine) -> None:
    """Create all Pro tables. Safe to call repeatedly (CREATE IF NOT EXISTS).

    Called from main.py at startup after the core tables are created.
    Uses Base.metadata.create_all with checkfirst=True so existing
    tables are left untouched.
    """
    from sqlalchemy.schema import CreateTable
    async with engine.begin() as conn:
        for table in [
            ProAuthor, ProTopic, ProArticleTopic, ProReadingHistory,
            ProBookmarkFolder, ProBookmarkItem, ProHighlight,
            ProReaction, ProCommentThread, ProPushSubscription,
            ProDigestSubscriber, ProCorrection, ProCitation, ProABTest,
        ]:
            try:
                await conn.run_sync(lambda sync_conn, t=table: t.__table__.create(sync_conn, checkfirst=True))
            except Exception as e:
                # Log but don't crash — Pro features degrade gracefully
                # if tables can't be created (e.g. permission issue).
                import logging
                logging.getLogger("pro_models").warning(
                    f"[ProModels] Failed to create {table.__tablename__}: {e}"
                )
