import re


SENTIMENT_ACCEPTED = "ACCEPTED"
SENTIMENT_REJECTED = "REJECTED"
DEFAULT_VALID_SENTIMENTS = {SENTIMENT_ACCEPTED, SENTIMENT_REJECTED}

POSITIVE_REACTION_PATTERN = re.compile(
    r"(?:👍|thumbs?[\s_-]*up|thumbsup|up[\s_-]*vote|\+\s*1|plus[\s_-]*one|\blike(?:d)?\b)",
    re.IGNORECASE,
)
NEGATIVE_REACTION_PATTERN = re.compile(
    r"(?:👎|thumbs?[\s_-]*down|thumbsdown|down[\s_-]*vote|-\s*1|minus[\s_-]*one|\bdislike(?:d)?\b)",
    re.IGNORECASE,
)

REACTION_DESCRIPTOR_KEYS = (
    "reaction",
    "reactionType",
    "reaction_type",
    "type",
    "name",
    "emoji",
    "emoticon",
    "label",
    "slug",
    "value",
)
REACTION_COUNT_KEYS = ("count", "total", "size", "votes")
REACTION_NESTED_KEYS = (
    "reaction",
    "reactions",
    "reactionSummary",
    "reactionsSummary",
    "emojiReactions",
    "properties",
    "metadata",
    "data",
    "details",
)


def _normalize_comment_id(comment_id):
    if comment_id is None:
        return None

    normalized_id = str(comment_id).strip()
    return normalized_id or None


def _coerce_reaction_count(value):
    if isinstance(value, bool):
        return 1 if value else 0

    if isinstance(value, (int, float)):
        return max(0, int(value))

    if isinstance(value, str):
        normalized_value = value.strip()
        if not normalized_value:
            return None

        if re.fullmatch(r"-?\d+", normalized_value):
            return max(0, int(normalized_value))

    if isinstance(value, list):
        return len(value)

    return None


def _reaction_sentiment_from_descriptor(descriptor):
    normalized_descriptor = str(descriptor or "").strip()
    if not normalized_descriptor:
        return None

    if POSITIVE_REACTION_PATTERN.search(normalized_descriptor):
        return SENTIMENT_ACCEPTED

    if NEGATIVE_REACTION_PATTERN.search(normalized_descriptor):
        return SENTIMENT_REJECTED

    return None


def _extract_reaction_entries(payload, depth=0, max_depth=4):
    if depth > max_depth:
        return []

    entries = []
    if isinstance(payload, list):
        for item in payload:
            entries.extend(
                _extract_reaction_entries(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            )
        return entries

    if not isinstance(payload, dict):
        return entries

    descriptor = ""
    for descriptor_key in REACTION_DESCRIPTOR_KEYS:
        descriptor_candidate = payload.get(descriptor_key)
        if descriptor_candidate is None:
            continue

        normalized_descriptor = str(descriptor_candidate).strip()
        if normalized_descriptor:
            descriptor = normalized_descriptor
            break

    if descriptor:
        descriptor_count = None
        for count_key in REACTION_COUNT_KEYS:
            count_candidate = _coerce_reaction_count(payload.get(count_key))
            if count_candidate is not None:
                descriptor_count = count_candidate
                break

        if descriptor_count is None:
            descriptor_count = _coerce_reaction_count(payload.get("users"))

        entries.append((descriptor, 1 if descriptor_count is None else descriptor_count))

    for nested_key in REACTION_NESTED_KEYS:
        if nested_key in payload:
            entries.extend(
                _extract_reaction_entries(
                    payload.get(nested_key),
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            )

    for key, value in payload.items():
        if (
            key in REACTION_DESCRIPTOR_KEYS
            or key in REACTION_COUNT_KEYS
            or key in REACTION_NESTED_KEYS
        ):
            continue

        key_sentiment = _reaction_sentiment_from_descriptor(key)
        if not key_sentiment:
            continue

        count_candidate = _coerce_reaction_count(value)
        if count_candidate is None and isinstance(value, dict):
            for count_key in REACTION_COUNT_KEYS:
                count_candidate = _coerce_reaction_count(value.get(count_key))
                if count_candidate is not None:
                    break

        entries.append((key, 1 if count_candidate is None else count_candidate))

    return entries


def _extract_reaction_counts(payload):
    totals = {SENTIMENT_ACCEPTED: 0, SENTIMENT_REJECTED: 0}
    for descriptor, count in _extract_reaction_entries(payload):
        sentiment = _reaction_sentiment_from_descriptor(descriptor)
        if not sentiment:
            continue

        normalized_count = _coerce_reaction_count(count)
        if normalized_count is None or normalized_count <= 0:
            continue

        totals[sentiment] += normalized_count

    return totals


def _extract_reaction_payloads(activity):
    if not isinstance(activity, dict):
        return []

    payloads = []
    for nested_key in REACTION_NESTED_KEYS:
        if nested_key in activity:
            payloads.append(activity.get(nested_key))

    if any(activity.get(key) is not None for key in REACTION_DESCRIPTOR_KEYS):
        payloads.append(activity)

    comment = activity.get("comment")
    if isinstance(comment, dict):
        for nested_key in REACTION_NESTED_KEYS:
            if nested_key in comment:
                payloads.append(comment.get(nested_key))

        if any(comment.get(key) is not None for key in REACTION_DESCRIPTOR_KEYS):
            payloads.append(comment)

    return payloads


def _extract_reaction_comment_id(activity, normalize_comment_id):
    if not isinstance(activity, dict):
        return None

    comment = activity.get("comment")
    if isinstance(comment, dict):
        comment_id = normalize_comment_id(comment.get("id"))
        if comment_id:
            return comment_id

    for direct_key in ("commentId", "comment_id"):
        direct_comment_id = normalize_comment_id(activity.get(direct_key))
        if direct_comment_id:
            return direct_comment_id

    for nested_key in ("targetComment", "parentComment"):
        nested_comment = activity.get(nested_key)
        if not isinstance(nested_comment, dict):
            continue

        nested_comment_id = normalize_comment_id(nested_comment.get("id"))
        if nested_comment_id:
            return nested_comment_id

    return None


def extract_reaction_sentiments_from_activities(
    activities,
    normalize_comment_id=None,
):
    resolved_normalize_comment_id = normalize_comment_id or _normalize_comment_id
    reaction_counts_by_comment_id = {}

    # Bitbucket reaction payload shapes vary by version/plugins,
    # so we aggregate from multiple likely fields and then apply a majority rule.
    for activity in activities or []:
        if not isinstance(activity, dict):
            continue

        comment_id = _extract_reaction_comment_id(
            activity,
            normalize_comment_id=resolved_normalize_comment_id,
        )
        if not comment_id:
            continue

        activity_totals = {SENTIMENT_ACCEPTED: 0, SENTIMENT_REJECTED: 0}
        for payload in _extract_reaction_payloads(activity):
            payload_totals = _extract_reaction_counts(payload)
            activity_totals[SENTIMENT_ACCEPTED] += payload_totals[SENTIMENT_ACCEPTED]
            activity_totals[SENTIMENT_REJECTED] += payload_totals[SENTIMENT_REJECTED]

        action_sentiment = _reaction_sentiment_from_descriptor(activity.get("action"))
        if action_sentiment and activity_totals[action_sentiment] == 0:
            activity_totals[action_sentiment] += 1

        if (
            activity_totals[SENTIMENT_ACCEPTED] <= 0
            and activity_totals[SENTIMENT_REJECTED] <= 0
        ):
            continue

        accumulated_totals = reaction_counts_by_comment_id.setdefault(
            comment_id,
            {SENTIMENT_ACCEPTED: 0, SENTIMENT_REJECTED: 0},
        )
        accumulated_totals[SENTIMENT_ACCEPTED] += activity_totals[SENTIMENT_ACCEPTED]
        accumulated_totals[SENTIMENT_REJECTED] += activity_totals[SENTIMENT_REJECTED]

    sentiment_by_comment_id = {}
    for comment_id, totals in reaction_counts_by_comment_id.items():
        positive_count = totals[SENTIMENT_ACCEPTED]
        negative_count = totals[SENTIMENT_REJECTED]

        if positive_count > negative_count:
            sentiment_by_comment_id[comment_id] = SENTIMENT_ACCEPTED
        elif negative_count > positive_count:
            sentiment_by_comment_id[comment_id] = SENTIMENT_REJECTED

    return sentiment_by_comment_id


def split_threads_by_reaction_sentiment(
    comment_threads,
    reaction_sentiment_by_comment_id,
    valid_sentiments=None,
    normalize_comment_id=None,
):
    resolved_valid_sentiments = set(valid_sentiments or DEFAULT_VALID_SENTIMENTS)
    resolved_normalize_comment_id = normalize_comment_id or _normalize_comment_id

    reaction_overrides = {}
    threads_for_llm = []

    for thread in comment_threads or []:
        comment_id = resolved_normalize_comment_id(thread.get("comment_id"))
        reaction_sentiment = reaction_sentiment_by_comment_id.get(comment_id or "")
        if reaction_sentiment in resolved_valid_sentiments and comment_id:
            reaction_overrides[comment_id] = reaction_sentiment
            continue

        threads_for_llm.append(thread)

    return threads_for_llm, reaction_overrides


def merge_thread_sentiments(
    llm_sentiment_by_comment_id,
    reaction_sentiment_overrides,
):
    merged = dict(llm_sentiment_by_comment_id or {})
    merged.update(reaction_sentiment_overrides or {})
    return merged
