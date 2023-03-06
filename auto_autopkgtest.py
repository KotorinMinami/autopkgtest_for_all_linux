import os , shutil
import re
import sys
import argparse
import paramiko
import subprocess
import time
from lib import sftp,ssh_cmd

class QemuVM(object):
    def __init__(self, vcpu=2,memory=2,workingDir='/',bkfile=None ,kernel=None,bios=None,id=1,port=12055,user='root',password='openEuler12#$', path='/root' , restore = True):
        self.id = id
        self.port , self.ip , self.user , self.password  = port , '127.0.0.1' , user , password
        self.vcpu , self.memory= vcpu , memory
        self.workingDir , self.bkFile = workingDir , bkfile
        self.kernel , self.bios = kernel , bios
        self.drive = 'img'+str(self.id)+'.qcow2'
        self.path = path
        self.restore = restore
        self.tapls = []
        if self.workingDir[-1] != '/':
            self.workingDir += '/'
    
    def start(self):
        self.port = findAvalPort(1)[0]
        if self.drive in os.listdir(self.workingDir):
            os.system('rm -f '+self.workingDir+self.drive)
        if self.restore:
            cmd = 'qemu-img create -f qcow2 -F qcow2 -b '+self.workingDir+self.bkFile+' '+self.workingDir+self.drive
            res = os.system(cmd)
            if res != 0:
                print('Failed to create cow img: '+self.drive)
                return -1
        ## Configuration
        memory_append=self.memory * 1024
        if self.restore:
            drive=self.workingDir+self.drive
        else:
            drive=self.workingDir+self.bkFile
        if self.kernel is not None:
            kernelArg=" -kernel "+self.kernel
        else:
            kernelArg=" "
        if self.bios is not None:
            if self.bios == 'none':
                biosArg=" -bios none"
            else:
                biosArg=" -bios "+self.workingDir+self.bios
        else:
            biosArg=" "
        ssh_port=self.port
        cmd="qemu-system-riscv64 \
        -nographic -machine virt  \
        -smp "+str(self.vcpu)+" -m "+str(self.memory)+"G \
        "+kernelArg+" \
        "+biosArg+" \
        -drive file="+drive+",format=qcow2,id=hd0 \
        -object rng-random,filename=/dev/urandom,id=rng0 \
        -device virtio-rng-device,rng=rng0 \
        -device virtio-blk-device,drive=hd0 \
        -device qemu-xhci -usb -device usb-kbd -device usb-tablet \
        -netdev user,id=usernet,hostfwd=tcp::"+str(ssh_port)+"-:22 -device virtio-net-device,netdev=usernet "
        print(cmd)
        self.process = subprocess.Popen(args=cmd,stderr=subprocess.PIPE,stdout=subprocess.PIPE,stdin=subprocess.PIPE,encoding='utf-8',shell=True)

    def waitReady(self):
        conn = 519
        while conn == 519:
            conn = paramiko.SSHClient()
            conn.set_missing_host_key_policy(paramiko.AutoAddPolicy)
            try:
                time.sleep(5)
                conn.connect(self.ip, self.port, self.user, self.password, timeout=5)
            except Exception as e:
                conn = 519
        if conn != 519:
            conn.close()

    def destroy(self):
        ssh_exec(self,'poweroff')
        if self.restore:
            os.system('rm -f '+self.workingDir+self.drive)
        os.system('rm -f '+self.workingDir+'disk'+str(self.id)+'-*')

def sftp_get(qemuVM,remotedir,remotefile,localdir,timeout=5):
    conn = paramiko.SSHClient()
    conn.set_missing_host_key_policy(paramiko.AutoAddPolicy)
    conn.connect(qemuVM.ip,qemuVM.port,qemuVM.user,qemuVM.password,timeout=timeout,allow_agent=False,look_for_keys=False)
    sftp.psftp_get(conn,remotedir,remotefile,localdir)

def findAvalPort(num=1):
    port_list = []
    port = 12055
    while(len(port_list) != num):
        if os.system('netstat -anp 2>&1 | grep '+str(port)+' > /dev/null') != 0:
            port_list.append(port)
        port += 1
    return port_list

def ssh_exec(qemuVM,cmd,timeout=5):
    conn = paramiko.SSHClient()
    conn.set_missing_host_key_policy(paramiko.AutoAddPolicy)
    conn.connect(qemuVM.ip,qemuVM.port,qemuVM.user,qemuVM.password,timeout=timeout,allow_agent=False,look_for_keys=False)
    exitcode,output = ssh_cmd.pssh_cmd(conn,cmd)
    ssh_cmd.pssh_close(conn)
    return exitcode,output

def get_apt_list(workDir , image , kernel):
    tempVM = QemuVM(workingDir=workDir , bkfile=image , id=1 , kernel=kernel , user='root' , password='openkylin')
    tempVM.start()
    tempVM.waitReady()
    print(ssh_exec(tempVM , 'apt update')[1])
    ssh_exec(tempVM , "apt list | awk -F/ '{print$1}' > apt_list")
    sftp_get(qemuVM=tempVM , remotedir='/root' , remotefile='apt_list' , localdir='.')
    tempVM.destroy()
    return 'apt_list'


if __name__ == "__main__":
    argv = sys.argv[1:]
    full_argv = ' '.join(argv)
    autopkgtest_argv = full_argv.split(' -- ')[1:]
    attach_argv = full_argv.split(' -- ')[0].split()
    
    parser = argparse.ArgumentParser()
    parser.add_argument('-l','--list' , type=str , dest="list" , default=None , help="Specify the test targets list")
    parser.add_argument('--image' , type=str , default=None , help="Specify backing image file name")
    parser.add_argument('-w','--workDir' , type=str , dest="workDir" , default='.' , help="Specify working directory , default is '.' , the test result will be store at workDir/testRes/testname")
    parser.add_argument('--src' , action='store_true' , default=True , help="Get the test source code , if ture , will be store at workDir/testSrc")
    parser.add_argument('-a' , action='store_true' , default=False , help="Get all the packade listed in apt list to test , if test targets list is specified , would use the target list")
    parser.add_argument('--kernel' , type=str , default=None , help="Specify the boot kernel , will append  to the autopkgtest qemu option and boot the qemuVM to get apt list")
    parser.add_argument('--destdir' , type=str , default='' , help="Specify the autopkgtest install destdir")
    args = parser.parse_args(attach_argv)
    kernel , workDir , image , apt_list = args.kernel , args.workDir , None , None
    destdir = args.destdir.rstrip('/')
    if args.image is None:
        print("please specify backing image file name")
        exit(1)
    else:
        image = args.image
    if args.list is not None:
        apt_list = args.list
    elif args.a and kernel is not None:
        apt_list = get_apt_list(workDir , image , kernel)
    
    try:
        os.mkdir(os.path.join(workDir , 'testRes'))
    except FileExistsError:
        pass
    if args.src: 
        try:
            os.mkdir(os.path.join(workDir , 'testSrc'))
        except FileExistsError:
            pass 
    if apt_list is not None:
        list_file = open(apt_list , 'r')
        raw = list_file.read()
        test_list = raw.split(sep="\n")
        list_file.close()

        test_list = [x.strip() for x in test_list if x.strip()!='' and x != 'Listing...']  #Remove empty elements
    else:
        test_list = []
    

    pkg_no_source = set()
    for test in test_list:
        if os.path.exists(os.path.join(workDir , 'testRes' , test)):
            continue
        else:
            os.mkdir(os.path.join(workDir , 'testRes' , test))
        Args = ' -o '+os.path.join(workDir , 'testRes' , test)
        srcDir = ''
        cmd = destdir+'/usr/bin/autopkgtest '+autopkgtest_argv[0]+' '+test+Args+' -- qemu '+autopkgtest_argv[1]+" --qemu-option='-machine virt -kernel "+kernel+"' "+workDir+image
        print("execute: "+cmd)
        proc = subprocess.Popen(cmd , shell=True , stdin=subprocess.PIPE , stdout=subprocess.PIPE , stderr=subprocess.PIPE)
        stderr = iter(proc.stderr.readline , b'')
        for line in stderr:
            line = line.decode('utf-8')
            print(line ,end='')
            if line == 'W: Unable to locate package '+test+'\n':
                pkg_no_source.add(test)
            elif args.src and not os.path.exists(os.path.join(workDir , 'testSrc' , test)):
                if srcDir == '' and re.search(r'autopkgtest-virt-qemu: DBG: executing copyup /tmp/(.*)/src/ (.*)/tests-tree/\n' , line) != None:
                    srcDir = os.path.join(workDir , 'testSrc' , test)
                elif srcDir == os.path.join(workDir , 'testSrc' , test):
                    os.mkdir(srcDir)
                    cpcmd = 'cp -r '+os.path.join(workDir , 'testRes' , test , 'tests-tree')+'/* '+srcDir
                    os.system(cpcmd)
            if proc.poll() is not None:
                if line == "":
                    break 
        if test in pkg_no_source:
            shutil.rmtree(os.path.join(workDir , 'testRes' , test))
        elif 'SKIP no tests in this package' in open(os.path.join(workDir , 'testRes' , test , 'summary') , 'r').read():
            if not os.path.exists(os.path.join(workDir , 'emptyTest')):
                os.mkdir(os.path.join(workDir , 'emptyTest'))
            os.system('mv '+os.path.join(workDir , 'testRes' , test)+" "+os.path.join(workDir , 'emptyTest'))
    with open('pkg_no_source' , 'w') as f:
        for pkg in pkg_no_source:
            f.write(pkg+'\n')

