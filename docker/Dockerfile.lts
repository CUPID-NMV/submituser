FROM htcondor/submit:lts

USER root

RUN mkdir -p /usr/local/share/ca-certificates /etc/condor

COPY htcondor_ca.crt /usr/local/share/ca-certificates/htcondor_ca.crt
COPY condor_config.local /etc/condor/condor_config.local

CMD ["/bin/bash"]
