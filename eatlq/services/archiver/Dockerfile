FROM rust:1.85-bookworm AS builder

WORKDIR /app
COPY . .
RUN cargo build --release -p archiver

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=UTC
WORKDIR /app

COPY --from=builder /app/target/release/archiver /usr/local/bin/archiver

CMD ["archiver"]
