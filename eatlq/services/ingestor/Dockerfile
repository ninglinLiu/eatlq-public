FROM rust:1.85-bookworm AS builder

WORKDIR /app
COPY . .
RUN cargo build --release -p ingestor --bins

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=UTC
WORKDIR /app

COPY --from=builder /app/target/release/ingestor /usr/local/bin/ingestor
COPY --from=builder /app/target/release/backfill /usr/local/bin/backfill
COPY --from=builder /app/target/release/backfill_enqueue_gap /usr/local/bin/backfill_enqueue_gap
COPY --from=builder /app/target/release/backfill_discover_gaps /usr/local/bin/backfill_discover_gaps
COPY --from=builder /app/target/release/backfill_scheduler /usr/local/bin/backfill_scheduler

CMD ["ingestor"]
