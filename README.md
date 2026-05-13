### starting htcondor propt
docker compose up -d
docker attach htcondor
### build and hand start
docker build -t htcondor-submit-ce:lts  -f Dockerfile.lts .
docker run --rm -it -v $PWD/submituser:/home/submituser -v /tmp/token:/tmp/token --name htcondor  htcondor-submit-ce:lts
