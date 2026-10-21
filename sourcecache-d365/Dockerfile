# Build context: this service's directory from scripts/build_services.py
# (its wheels, requirements.txt and a copy of this file). Installs offline and
# hash-checked; the base image's Python must match the build's --python-version,
# which build_services.py writes into the default below.
ARG PYTHON_IMAGE=python:3.14-slim
FROM ${PYTHON_IMAGE}
COPY *.whl requirements.txt /build/
RUN python -m pip install --no-cache-dir --no-index --require-hashes --find-links /build -r /build/requirements.txt \
 && python -m pip check && rm -rf /build
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["boring-sourcecache-d365"]
