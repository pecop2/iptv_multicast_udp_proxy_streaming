FROM python:3.7

COPY *.py /workdir/

COPY ./requirements.txt /workdir/requirements.txt

WORKDIR /workdir

RUN apt update
RUN apt install vlc -y

RUN python -m pip install --upgrade pip
RUN pip install -r requirements.txt
EXPOSE 8010 8011


CMD python -u udp_multicast_proxy.py
