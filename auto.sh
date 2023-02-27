#!/bin/sh

mkdir src
mkdir testing
while read line
do
    bash sshcmd.sh -c "apt-get source $line" -i 127.0.0.1 -p openkylin -u root -o $1 > temp
    grep "E: Unable to find a source package" temp
    if [ $? -eq 0 ];then
        echo "$line" >> pkg_no_source
        continue
    fi
    bash sshscp.sh -s root@localhost:/root/* -d ./src -p openkylin -o $1 >temp
    pkg_dsc=$(grep ".dsc" temp | awk '{print$1}')
    pkg_dsc=${pkg_dsc:1}
    mkdir ./testing/${pkg_dsc%%_*}
    
    
    $5/usr/bin/autopkgtest ./src/$pkg_dsc -o ./testing/${pkg_dsc%%_*} \
    -d -B -- qemu -u root -p openkylin --qemu-architecture=riscv64 -c 8 --ram-size=8192 \
    -d '--qemu-options=-machine virt -kernel '$3'/u-boot.bin' $3'/'$4
    
    
    grep "SKIP no tests in this package" ./testing/"${pkg_dsc%%_*}"/summary
    if [ $? -eq 0 ];then
        echo "$line" >> pkg_no_test
    else 
        echo $line >> pkg_test_finish
    fi
    rm -rf ./src/*
    bash sshcmd.sh -c "rm -rf /root/*" -i 127.0.0.1 -p openkylin -u root -o $1
done < $2

