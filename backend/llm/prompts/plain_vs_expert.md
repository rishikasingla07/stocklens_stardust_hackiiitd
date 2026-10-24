Tone instructions for explain.md. backend/llm/explain.py inserts the text
under the matching heading into {tone_instructions}. Keep the two headings
exactly as written.

## plain

Write for a retail investor with no finance training.
- At most 120 words, in two short paragraphs.
- Short sentences and everyday words. Explain any term you must use, e.g.
  "an exchange filing (an official notice the company sends to the NSE)".
- Say the confidence as the label in words ("High confidence"). Mention the
  score only once, if at all, as "{confidence_score} out of 100".
- Give times in IST, like "9:48 am, 14 minutes before the fall began".

## expert

Write for an experienced trader or analyst.
- At most 200 words, in one to three paragraphs.
- Use standard market terms (Tier 1 filing, corroboration, relative to
  sector and index) without explaining them.
- Give the confidence label and the score, and name the score breakdown
  rules that added or removed points, with their points as listed.
- Give exact IST timestamps and the gap to the move start, the stock's move
  against the sector and index, and the volume multiple.
- Name each rejected item and its reason in a few words.
