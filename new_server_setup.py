import getpass
from fabric import Config, Connection

sudo_pass = getpass.getpass("Enter sudo password: ")
config = Config(overrides={"sudo": {"password": sudo_pass}})


def install_nginx():
    c = Connection("staging.skysmuggler.com", config=config)
    c.sudo("apt update", pty=True)
    c.sudo("apt install nginx", pty=True)


if __name__ == "__main__":
    install_nginx()

