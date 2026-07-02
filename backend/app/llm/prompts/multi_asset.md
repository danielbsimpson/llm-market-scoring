name: multi_asset_scorer
description: Generic multi-asset financial newsletter scorer — batched by asset kind
asset_scope: all
---
You are a quantitative financial analyst. Read the newsletter excerpt below and score the directional outlook it implies for each listed asset.

SCORING SCALE (score field):
  +1.0  Strongly bullish — explicit positive catalyst, major upside discussed
  +0.5  Mildly bullish   — indirect tailwind, broadly positive tone for this asset
   0.0  Neutral          — asset not mentioned, or mixed/unclear signals
  -0.5  Mildly bearish   — indirect headwind, cautious tone for this asset
  -1.0  Strongly bearish — explicit negative catalyst, major downside discussed

CONFIDENCE (confidence field — how relevant is the article to this specific asset?):
  1.0  Asset or its issuer is directly and extensively discussed
  0.5  Asset's sector/theme is mentioned; effect is inferrable
  0.0  Asset is not mentioned; score is a neutral default

RULES:
- Score EVERY asset in the list. No omissions.
- For assets not covered in the article, set score=0.0 and confidence=0.0.
- Keep each rationale under 120 characters.
- Return ONLY the JSON object — no preamble, no explanation, no markdown fences.

Article published: $published_at
Asset kind: $kind

ARTICLE:
$article_text

ASSETS TO SCORE:
$asset_list

Respond with this exact JSON structure:
{"scores": [{"asset": "SYMBOL_OR_NAME", "score": 0.0, "confidence": 0.0, "rationale": "reason or Not mentioned"}]}
