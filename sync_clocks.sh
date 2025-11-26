#!/bin/bash

sudo systemctl stop chrony
sudo chronyd -q 'server pool.ntp.org iburst'
sudo systemctl start chrony
