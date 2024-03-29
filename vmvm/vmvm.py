import logging, yaml, subprocess, io, sys, os, socket
from logging import info,warning,error
from importlib import resources

from .lib.tpm_manager import TPMManager


presets_resource_location = os.path.join(resources.files(__package__), 'presets.yml')
presets = yaml.safe_load(open(presets_resource_location, 'r'))


def is_port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(('localhost',port))
        return True
    except:
        pass
    return False

def find_next_free_port(port: int) -> int:
    while not is_port_free(port):
        port += 1
    return port


class App:
    def __init__(self, conf_dir: str):
        os.chdir(conf_dir)
        conf = yaml.safe_load(open('vmconfig.yml', 'r'))

        self._load_conf(conf)



    def _load_conf(self, conf: dict) -> None:

        def _wrap_scalar_as_list(scalar_or_list_value):
            return scalar_or_list_value if type(scalar_or_list_value) == list else [scalar_or_list_value]

        if 'disk' in conf:
            self.disks = _wrap_scalar_as_list(conf['disk'])
        elif 'disks' in conf:
            self.disks = _wrap_scalar_as_list(conf['disks'])
        else:
            self.disks = []

        self.first_disk = self.disks[0] if len(self.disks) > 0 else None

        current_preset = presets[conf['preset']] if 'preset' in conf else presets['default']

        for k,v in current_preset.items():
            # overwrite with preset value if not set in conf
            if k not in conf:
                conf[k] = v

        self.name = conf['name']
        self.cpus = conf['cpus']
        self.ram = conf['ram']
        self.arch = conf['arch']
        self.machine =  {
            'i386':    'pc',  #  machine type corresponding to late 90s - early 2000s era
            'x86_64':  'q35',
            'aarch64': 'virt',
        }[self.arch]
        self.spice_port = conf['spice_port'] if 'spice_port' in conf else 'auto'
        self.enable_efi = conf['efi'] if 'efi' in conf else False
        self.edk2_subdir = {
            'i386':    None,
            'x86_64':  'x64',
            'aarch64': 'aarch64',
            }[self.arch]
        self.enable_boot_menu = conf['bootmenu'] if 'bootmenu' in conf else False
        self.enable_secureboot = conf['secureboot'] if 'secureboot' in conf else False
        self.enable_tpm = conf['tpm'] if 'tpm' in conf else False
        self.disk_virtio_mode = conf['disk_virtio'] if 'disk_virtio' in conf else 'blk'
        self.usbdevices =_wrap_scalar_as_list(conf['usb']) if 'usb' in conf else []
        self.share_dir_as_fat = conf['share_dir_as_fat'] if 'share_dir_as_fat' in conf else None
        self.share_dir_as_fsd = conf['share_dir_as_fsd'] if 'share_dir_as_fsd' in conf else None

        if 'nic' in conf:
            match conf['nic']:
                case 'virtio':
                    self.nic_model = 'virtio-net-pci'
                case 'none':
                    self.nic_model = None
                case _:
                    self.nic_model = conf['nic']
        else:
            self.nic_model = None

        self.nic_forward_ports = conf['nic_forward_ports'] if 'nic_forward_ports' in conf else None

        if 'sound' in conf:
            match conf['sound']:
                case 'none':
                    self.soundcard_model = None
                case _:
                    self.soundcard_model = conf['sound']
        else:
            self.soundcard_model = None

        self.vga = conf['vga'] if 'vga' in conf else 'qxl'
        self.control_socket = conf['control_socket'] if 'control_socket' in conf else False

        self.isoimages = _wrap_scalar_as_list(conf['os_install']) if 'os_install' in conf else []
        self.local_hw_arch = subprocess.check_output(['uname', '-m']).decode().rstrip()


    def _exec(self, executable_name: str, args: list[str]) -> int:
        real_args = [executable_name] + args
        info('running %s with args: %s', executable_name, " ".join(real_args))
        proc = subprocess.Popen(args=real_args,stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        info('-'*80)
        for line in io.TextIOWrapper(proc.stdout, encoding='utf-8'):
            info(line.rstrip())
        exit_code = proc.wait()
        info('-'*80)
        info('%s exited with code %d', executable_name, exit_code)
        return exit_code

    def _start_tpm(self):
        if self.enable_tpm:
            info('starting software TPM daemon')
            self.tpm_manager = TPMManager(self.name)
            self.tpm_manager.run()

    def _shutdown_tpm(self):
        if self.enable_tpm:
            info('shutting down software TPM daemon')
            self.tpm_manager.shutdown()


    def act_init(self):
        info('action: initializing vm')

        if self.first_disk is not None:
            if os.path.exists(self.first_disk):
                error('disk already exists, not overwriting: %s', self.first_disk)
                return
            else:
                self._exec('qemu-img', ['create', '-f', 'qcow2', self.first_disk, '100G'])
        else:
            error('no disks configured?')

    def _common_args(self):
        spice_port = self.spice_port
        if spice_port == 'auto':
            spice_port = find_next_free_port(5900)
        info(f'SPICE server running on localhost:{spice_port}')


        # Common Args
        args = [
            '-name', self.name,
            '-machine', self.machine,
            '-smp', str(self.cpus),
            '-m', self.ram,

            # GUI:
            '-display', 'gtk,show-cursor=off',
            #'-display', f'sdl,gl=off',

            # SPICE:
            '-spice', f'port={spice_port},disable-ticketing=on,gl=off',
            '-device', 'virtio-serial-pci',
            '-device', 'virtserialport,chardev=spicechannel0,name=com.redhat.spice.0',
            '-chardev', 'spicevmc,id=spicechannel0,name=vdagent',

            #'-balloon', 'virtio',
            #'-localtime',
        ]

        if self.control_socket:
            args += [ '-qmp', 'unix:qmp.sock,server,nowait', ]



        # Sound Card (and PC speaker)
        match self.soundcard_model:
            case 'spk':
                args += [ '-audiodev', 'pa,id=spk0', '-machine', 'pcspk-audiodev=spk0',   ]
            case 'sb16':
                args += [ '-audiodev', 'pa,id=snd0', '-device', 'sb16,audiodev=snd0', ]
            case 'ac97':
                args += [ '-audiodev', 'pa,id=snd0', '-device', 'ac97,audiodev=snd0', ]
            case 'hda':
                args += ['-audiodev', 'pa,id=snd0', '-device', 'ich9-intel-hda', '-device', 'hda-output,audiodev=snd0',]

        # USB
        if self.machine == 'pc':
            args += [
                '-usb',
                '-device', 'usb-tablet',
            ]
        else:
            args += [
                '-device', 'qemu-xhci',
                '-device', 'usb-tablet',
            ]


        # CPU and accel type
        # check if host hardware has same arch as the guest so that KVM can be enabled
        if self.local_hw_arch == self.arch:
            args += [
                '-enable-kvm',
                '-cpu', 'host',
            ]
        elif self.local_hw_arch == 'x86_64'  and self.arch == 'i386':
              args += [
                '-enable-kvm',
                '-cpu', 'host',
            ]
        else:
            args += [
                '-cpu', 'max',
            ]

        args += [ '-vga', self.vga ]

        # EFI
        if self.enable_efi:
            edk2_path = f'/usr/share/edk2/{self.edk2_subdir}/'

            if os.path.exists(f'{edk2_path}OVMF_CODE.fd') and os.path.exists(f'{edk2_path}OVMF_VARS.fd'):
                efi_prefix = 'OVMF'
            elif os.path.exists(f'{edk2_path}QEMU_CODE.fd') and os.path.exists(f'{edk2_path}QEMU_VARS.fd'):
                efi_prefix = 'QEMU'
            else:
                efi_prefix = None


            if efi_prefix is not None:

                if not os.path.exists(f'./{efi_prefix}_VARS.fd'):
                    info(f'{efi_prefix}_VARS.fd file does not exist in VM directory, copying from system')
                    self._exec('cp', [f'{edk2_path}{efi_prefix}_VARS.fd', '.'])

                efi_fd_variant = 'secboot.fd' if self.enable_secureboot else 'fd'

                args += [
                    '-drive', f'if=pflash,format=raw,readonly=on,file={edk2_path}{efi_prefix}_CODE.{efi_fd_variant}',
                    '-drive', f'if=pflash,format=raw,file={efi_prefix}_VARS.fd',
                ]

            else:
                #error('edk2 files not found. Please install edk2-ovmf package.')
                package_name_suffix = {
                    'x86_64':  'ovmf',
                    'aarch64': 'aarch64',
                }[self.arch]
                raise FileNotFoundError(f'edk2 files not found. Please install edk2-{package_name_suffix} package.')


        # TPM
        if self.enable_tpm:
            args += [
                '-chardev', f'socket,id=chrtpm,path={self.tpm_manager.sock}', '-tpmdev', 'emulator,id=tpm0,chardev=chrtpm', '-device', 'tpm-tis,tpmdev=tpm0',
            ]


        def generate_blockdev_desc(idx: int, filename: str,disk_virtio_mode: str) -> list[str]:
            if '/dev' in filename:
                d = [
                    '-drive', f'file={filename},format=raw,media=disk',
                ]
            else:
                d = [
                    '-drive', f'file={filename},if=none,id=hd{idx}',
                ]

                if disk_virtio_mode == 'scsi':
                    d += [
                        '-device', f'scsi-hd,drive=hd{idx},bootindex={idx+1}',
                    ]
                elif disk_virtio_mode == 'blk':
                    d += [
                        '-device', f'virtio-blk-pci,id=virtblk{idx},num-queues=4,drive=hd{idx},bootindex={idx+1}',
                        ]
                else:
                    d += [
                        '-device', f'ide-hd,drive=hd{idx},bootindex={idx+1}',
                    ]
            return d

        # disks
        if self.disk_virtio_mode == 'scsi':
            args += [ '-device', 'virtio-scsi-pci,id=scsi0,num_queues=4' ]
        for idx in range(len(self.disks)):
            args += generate_blockdev_desc(idx, self.disks[idx], self.disk_virtio_mode)

        for usb_dev in self.usbdevices:
            vendor_id,product_id = usb_dev.split(':')
            args += [ '-device', f'usb-host,vendorid=0x{vendor_id},productid=0x{product_id}' ]

        # FAT drive share
        if self.share_dir_as_fat is not None:
            args += [
                # '-blockdev', f'driver=vfat,file=fat:ro:{self.share_dir_as_fat},id=vfat,format=raw,if=none',
                # '-device', 'usb-storage,drive=vfat',

                #'-hda', f'fat:ro:{self.share_dir_as_fat}',

                '-drive', f'file=fat:rw:{self.share_dir_as_fat},format=raw,media=disk',
            ]

        # virtiofsd share
        if self.share_dir_as_fsd is not None:
            dir_abs = os.path.abspath(self.share_dir_as_fsd)
            args += [
                '-fsdev', f'local,security_model=passthrough,id=fsdev0,path={dir_abs}',
                '-device', 'virtio-9p-pci,fsdev=fsdev0,mount_tag=hostshare',
            ]

        # net
        # https://wiki.qemu.org/Documentation/Networking

        if self.nic_model is not None:
            if self.nic_forward_ports is not None:
                hostfwd_list = list(map(lambda bind_spec: f"hostfwd=tcp::{bind_spec['host']}-:{bind_spec['guest']}" , self.nic_forward_ports))
                hostfwd = ','+','.join(hostfwd_list)
            else:
                hostfwd = ''
            args += ['-netdev', f'user,id=net0{hostfwd}', '-device', f'{self.nic_model},netdev=net0' ]
        else:
            args += [ '-nic', 'none' ]

        return args

    def _boot_args(self, mode: str):
        args = []
        if self.enable_boot_menu:
            args = [ '-boot', 'menu=on', ]
        else:
            if mode == 'install':
                # boot from CD-ROM first, switch back to default order after reboot
                args = [ '-boot', 'once=d', ]
            elif mode == 'run':
                args = [ '-boot', 'order=c', ]
        return args


    def act_install(self):
        self._start_tpm()
        info('action: installing operating system inside vm')
        args = self._common_args() + self._boot_args(mode='install')
        for isofile in self.isoimages:
            isofile = isofile.replace(',', ',,') # double every ',' to escape it
            args += ['-drive', f'file={isofile},media=cdrom' ]
        self._exec(f'qemu-system-{self.arch}', args)
        self._shutdown_tpm()


    def act_run(self):
        self._start_tpm()
        info('action: running vm')
        args = self._common_args() + self._boot_args(mode='run')
        self._exec(f'qemu-system-{self.arch}', args)
        self._shutdown_tpm()




def usage():
    print('A very simple shell for setting up and running QEMU virtual machines from yaml config')
    print()
    print('usage: vmvm ACTION [VM_CONF_DIR]')
    print()
    print('  ACTION = init | install | run')
    print('    init        create virtual disks')
    print('    install     boot from os_install device to install operating system')
    print('    run         boot from first hard disk')
    print()
    print('  VM_CONF_DIR   dir must contain vmconfig.yml. All paths in config are relative to this dir. Uses current dir by default.')


def main():
    logging.basicConfig(format='%(asctime)s  %(levelname)s  %(message)s', level=logging.INFO)

    cmd = None
    dir_name = './'
    for arg in sys.argv[1:]:
        if arg in ['init', 'install', 'run']:
            cmd = arg
        else:
            dir_name = arg

    if cmd is None:
        error('action not specified')
        usage()
        return

    if dir_name is None:
        error('vm conf directory not specified')
        usage()
        return

    app = App(dir_name)
    getattr(app,'act_'+cmd)()


if __name__ == '__main__':
    main()
