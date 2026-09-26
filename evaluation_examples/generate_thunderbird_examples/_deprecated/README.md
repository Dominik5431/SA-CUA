# Deprecated generators

Moved here 2026-09-10. Superseded by `../generate_unsubscribe_newsletters.py`.

| file | what it made |
|---|---|
| `generate_unsubscribe_single.py` | `thunderbird-unsubscribe-single` -- 1 newsletter (TechDaily Digest), 5 mails, **benign only** |
| `generate_unsubscribe_multi.py`  | `thunderbird-unsubscribe-multi` -- 3 newsletters (TechDaily/DealsWeekly/CodeBytes), 8 mails, **benign only** |

Both are thin wrappers over `generate_thunderbird_example_template.py` with no CLI and no
attack. Their outputs live in `evaluation_examples/examples/custom/` and are referenced by
no `test_all_*` index, so nothing runs them.

**Do not re-run them as-is.** Neither overrides `tpl.ADVERSARIAL_EVALUATOR`, which defaults to
the Lisa Wong *phishing* evaluator inherited from the template's original purpose. Regenerating
would staple that onto an unsubscribe task, pointed at `/tmp/mock_smtp_outbox/` -- a path these
tasks never create (they set `INCLUDE_MOCK_SMTP = False`), so it would always score 0.0. The
committed configs had it stripped by hand. If you revive either, add `tpl.ADVERSARIAL_EVALUATOR
= None` first.
