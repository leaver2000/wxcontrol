# syntax=docker/dockerfile:1
# wxserver: API (uvicorn) and ingest worker share this image.

ARG PYTHON=3.12

# ---- build: compile nzthermo (C++20 + Cython + OpenMP) into a wheel ----------------------------
FROM python:${PYTHON}-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*
# nzthermo's _ufunc.pyx uses `bint` ufuncs that Cython >= 3.1 rejects, and pip >= 25.2 no longer
# applies constraints to isolated build environments, so build without isolation on Cython 3.0.x.
ARG NZTHERMO_REF=3435f97683ec826ea1ee625cfc95c98ac38a8381
RUN pip install --no-cache-dir "Cython==3.0.11" "numpy>=2" setuptools setuptools_scm wheel \
    && pip wheel --no-cache-dir --no-deps --no-build-isolation -w /wheels \
       "git+https://github.com/leaver2000/nzthermo@${NZTHERMO_REF}"
COPY pyproject.toml /src/pyproject.toml
COPY wxserver /src/wxserver
RUN pip wheel --no-cache-dir -w /wheels /src

# ---- runtime -----------------------------------------------------------------------------------
FROM python:${PYTHON}-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 wx
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER wx
WORKDIR /home/wx
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=4
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)" || exit 1
CMD ["uvicorn", "wxserver.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
