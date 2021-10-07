FROM python:3.7

COPY *.py /workdir/

COPY ./requirements.txt /workdir/requirements.txt

WORKDIR /workdir

RUN apt update
RUN apt install vlc -y

RUN python -m pip install --upgrade pip
RUN pip install -r requirements.txt
EXPOSE 8000 8001


CMD python -u udp_multicast_proxy.py
