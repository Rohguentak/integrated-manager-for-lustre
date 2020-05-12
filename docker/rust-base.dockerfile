# syntax=docker/dockerfile:experimental

FROM centos:7
WORKDIR /build
RUN yum update -y \
  && yum install -y gcc openssl openssl-devel epel-release https://download.postgresql.org/pub/repos/yum/reporpms/EL-7-x86_64/pgdg-redhat-repo-latest.noarch.rpm \
  && yum install -y postgresql96-devel cargo \
  && yum clean all

ENV PATH $PATH:/root/.cargo/bin
ENV CARGO_HOME /root/.cargo
ENV RUSTUP_HOME /root/.rustup
ENV PQ_LIB_DIR=/usr/pgsql-9.6/lib
COPY . .
RUN --mount=type=cache,target=/root/.cargo,sharing=private \
  --mount=type=cache,target=/root/target \
  cargo build --release --target-dir=/root/target \
  && mkdir -p /build/target/release \
  && cp -R /root/target/release/* /build/target/release/