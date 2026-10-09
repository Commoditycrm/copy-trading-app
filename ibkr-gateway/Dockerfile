# IBKR Client Portal Gateway — one container per IBKR login (Kopyya-hosted).
#
# IBKR gives individual accounts API access only through this Java gateway,
# signed into by the account owner once per trading day. Hosting it here means
# a subscriber installs nothing: the backend reaches the container over the
# compose network (https://ibkr-gw-N:5000) and the owner signs in through the
# backend-proxied login page (services/ibkr_hosted_gateway.py).
#
# The zip is IBKR's current build at image build time; rebuild to pick up a
# newer gateway. conf.yaml is ours: the allow-list admits compose-network
# addresses only.
FROM eclipse-temurin:21-jre-jammy

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /gateway
RUN curl -fsSL -o /tmp/gw.zip https://download2.interactivebrokers.com/portal/clientportal.gw.zip \
 && unzip -q /tmp/gw.zip -d /gateway \
 && rm /tmp/gw.zip \
 && chmod +x /gateway/bin/run.sh
COPY conf.yaml /gateway/root/conf.yaml

RUN useradd -r -u 10001 -d /gateway gateway && chown -R gateway /gateway
USER gateway

EXPOSE 5000
# 200 (login page) or 302 (redirect to it) both mean the gateway is up; whether
# anyone is signed in is the backend's concern, not the container's health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -sk -o /dev/null -w '%{http_code}' https://localhost:5000/sso/Login | grep -qE '^(200|302)$' || exit 1

CMD ["sh", "-c", "cd /gateway && exec bin/run.sh root/conf.yaml"]
