# Tennis Abstract Analyst

FastAPI service for Tennis Abstract player match-log retrieval and surface/venue statistics.

## Endpoints
- `GET /health`
- `GET /player/{player_name}?recent_n=10`
- `GET /docs`

## Metrics
Aces, double faults, first-serve percentage, first-serve points won, second-serve points won, service games won, break points saved, return points won and return games won. Results and averages are split into Hard, Clay and Grass, with Indoor, Outdoor and Unknown venue buckets.

## Run locally
`pip install -r requirements.txt && uvicorn app:app --reload`

## Data integrity
Missing values are null/N/A. Venue is Unknown unless explicitly verified. This initial integration must be validated against the current Tennis Abstract page structure before relying on the statistics.