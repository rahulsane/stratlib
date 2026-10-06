# Jev fixture provenance

`choice_response.json` is a synthetic contract fixture, not a recorded paid
evaluation. Its envelope follows Vercel's TypeSafe-compatible API documentation
and its Choice fields follow TypeSafe's API reference, checked 2026-09-29:

- https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe
- https://docs.typesafe.ai/api

The invented answers deliberately disagree with passing handle rules. This
tests that advisory results cannot change those rules. Probabilities and
confidence are test values, not measured model quality. Tests also use the
existing recorded FMP weekly price evidence to validate request construction.
No live Jev request was made to create this synthetic fixture.

## Recorded live response

`recorded_APA_2026-09-29.json` contains the response fields recorded from the
user-authorized live GUI test on 2026-09-29 at 05:33:58 UTC. The request used
APA's cached prices through 2026-09-25 and its 23-week cup-with-handle base
from 2026-03-30 through 2026-09-04. The response carries all three named Choice
answers, usage and Gateway routing/cost metadata. It contains no credentials.

The app made one HTTP request. Gateway internally retried the same Jev model
through TypeSafe after DigitalOcean returned 503. It returned the alias
`typesafe-ai/jev`, not an underlying model version. This fixture records actual
answers, not a claim about their accuracy. Its regression test replays locally
with the existing recorded FMP APA bars and never contacts the live API.
