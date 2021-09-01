# py-sss-pts

Python implementation of
[Solid State Storage (SSS) Performance Test Specification (PTS)](https://www.snia.org/tech_activities/standards/curr_standards/pts)

Tested with:
- lspci version 3.5.6
- lshw version B.02.19.2
- nvme version 1.10
- fio 3.23

Currently NVMe only (tested with Samsung SSD 970 EVO Plus 500GB)

Obviously all utilities mentioned above must be installed

Sudo must be configured to allow lspci, lshw, nvme, fio without password:
```
# cat /etc/sudoers.d/vadim 
vadim ALL=(ALL) NOPASSWD:/usr/bin/fio,/usr/sbin/nvme,/usr/sbin/lshw,/sbin/lspci
```
