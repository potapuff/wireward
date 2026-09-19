FROM golang:1.23-bookworm AS build
WORKDIR /src
# go.sum фіксує версії й контрольні суми: збірка відтворювана, а не «що сьогодні
# видасть проксі модулів». go mod tidy тут навмисно НЕ виконується.
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/ ./cmd/
COPY internal/ ./internal/
RUN CGO_ENABLED=0 go build -trimpath -mod=readonly -o /out/pgproxy ./cmd/pgproxy

FROM debian:bookworm-slim
RUN useradd --system --uid 10002 --no-create-home pgproxy \
 && mkdir -p /var/lib/pgproxy && chown 10002 /var/lib/pgproxy
COPY --from=build /out/pgproxy /usr/local/bin/pgproxy
USER 10002
ENTRYPOINT ["/usr/local/bin/pgproxy"]
