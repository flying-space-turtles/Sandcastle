# syntax=docker/dockerfile:1.6
FROM alpine:3.19

RUN apk add --no-cache \
    iptables \
    ip6tables \
    iproute2 \
    bash

COPY docker/firewall/rules.sh /etc/firewall/rules.sh
RUN chmod +x /etc/firewall/rules.sh

CMD ["/etc/firewall/rules.sh"]
