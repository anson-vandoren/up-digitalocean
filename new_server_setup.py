import getpass
import os
import re
import sys
from io import BytesIO, StringIO
from string import Template

import requests
from fabric import Config, Connection
from invoke import Responder

SERVER_NAME = os.environ.get("FABRIC_SERVER_NAME", None)
if SERVER_NAME is None:
    SERVER_NAME = input("Enter server name (or set envar FABRIC_SERVER_NAME): ")

EMAIL_ADDRESS = os.environ.get("FABRIC_EMAIL_ADDRESS", None)
if EMAIL_ADDRESS is None:
    EMAIL_ADDRESS = input(
        "Enter your email address for Certbot registration (or set envar FABRIC_EMAIL_ADDRESS): "
    )
DO_HTTPS_REDIRECT = True
# If you don't want to type your sudo password each time, you can set it via environment variable instead
sudo_pass = os.environ.get("FABRIC_SUDO_PASSWORD", None)
if sudo_pass is None:
    sudo_pass = getpass.getpass(
        "Enter sudo password (or set envar FABRIC_SUDO_PASSWORD): "
    )

CONFIG = Config(overrides={"sudo": {"password": sudo_pass}})


def basic_setup():
    # Initial connection needs to be over root (using predetermined ssh key)
    b = Connection(SERVER_NAME, user="root", config=CONFIG)
    new_user = os.getlogin()
    for userline in b.run("cat /etc/passwd", hide=True).stdout.split("\n"):
        if new_user in userline:
            print(f"User '{new_user}' already exists'")
            break
    else:
        print(
            f"Creating new user named '{new_user}' with sudo privileges on {SERVER_NAME}"
        )
        b.run(f"adduser {new_user}")
    b.run(f"usermod -aG sudo {new_user}")
    print("Enabling firewall for ssh")
    b.run("ufw allow OpenSSH && ufw enable && ufw status")
    print(f"Copying ssh key to {new_user} authorized_keys")
    b.run(f"rsync --archive --chown={new_user}:{new_user} ~/.ssh /home/{new_user}")
    print("Closing connection as root...")
    b.close()


def install_nginx(c):
    """https://www.digitalocean.com/community/tutorials/how-to-install-nginx-on-ubuntu-18-04"""
    print("Checking for Nginx...")

    nginx_installed = c.run("dpkg -s nginx", warn=True)
    for line in nginx_installed.stdout.split("\n"):
        if "Status: install ok installed" in line:
            nginx_installed = True
            break
    else:
        nginx_installed = False

    if not nginx_installed:
        print("\tInstalling Nginx...")
        c.sudo("apt update")
        c.sudo("apt install nginx -y")
        print("\t...done installing")
    else:
        print("...Nginx already installed")

    print("Setting up server block...")
    c.sudo(f"mkdir -p /var/www/{SERVER_NAME}/html")
    c.sudo(f"chown -R $USER:$USER /var/www/{SERVER_NAME}/html")
    c.sudo(f"sudo chmod -R 755 /var/www/{SERVER_NAME}")
    with open("server_block_template") as block:
        block_file = Template(block.read())
        block_file = block_file.substitute({"hostname": SERVER_NAME})
        # Two-step transfer and then move to position since there is
        # no sudo Transfer.put()
        c.put(StringIO(block_file), "tmp_server_conf")
        c.sudo(f"mv tmp_server_conf /etc/nginx/sites-available/{SERVER_NAME}")

    # Check whether a symbolic link already exists for this server block:
    if not c.run(f"cat /etc/nginx/sites-enabled/{SERVER_NAME}", warn=True):
        c.sudo(
            f"ln -s /etc/nginx/sites-available/{SERVER_NAME} /etc/nginx/sites-enabled/"
        )
    else:
        print("\tsymlink already exists for this server block")

    nginx_conf = BytesIO()
    c.get("/etc/nginx/nginx.conf", nginx_conf)
    nginx_conf = nginx_conf.getvalue().decode("utf-8")
    # enable bucket hash size
    nginx_conf = re.sub(
        "# server_names_hash_bucket_size 64;",
        "server_names_hash_bucket_size 64;",
        nginx_conf,
    )
    c.put(StringIO(nginx_conf), "tmp_nginx_conf")
    c.sudo("mv tmp_nginx_conf /etc/nginx/nginx.conf")

    # Test Nginx config
    config_test = c.sudo("nginx -t").stderr
    for line in config_test:
        if "syntax is ok":
            print("\tNginx config syntax is OK")
            break
    else:
        print("\tNginx configuration is not OK: \n", config_test)
        exit()

    print("\tRestarting Nginx")
    c.sudo("systemctl restart nginx")
    print("...done setting up Nginx")


def install_lets_encrypt(c):
    print("Checking firewall status for LetsEncrypt...")
    ufw_status = c.sudo("ufw status")
    for line in ufw_status.stdout.split("\n"):
        if re.search(r"Nginx Full.+ALLOW.+Anywhere", line):
            print("...ufw already enabled for Nginx Full")
            break
    else:
        print("\tUpdating ufw for full Nginx to allow for LetsEncrypt...")
        c.sudo("ufw allow 'Nginx Full'")
        print("\t...done updating ufw")

    print("Adding Certbot repositories and installing Certbot")
    c.sudo("apt-get install software-properties-common -y")
    c.sudo("add-apt-repository universe -y")
    c.sudo("add-apt-repository ppa:certbot/certbot -y")
    c.sudo("apt-get update")
    c.sudo("apt install python-certbot-nginx -y")

    email_responder = Responder(
        pattern=r"Enter email address \(used for urgent renewal and security notices\)",
        response=f"{EMAIL_ADDRESS}\n",
    )
    tos_responder = Responder(pattern=r"\(A\)gree/\(C\)ancel: ", response="A\n")
    redirect_responder = Responder(
        pattern=r"Select the appropriate number \[1-2\] then \[enter\]",
        response=f"{'2' if DO_HTTPS_REDIRECT else '1'}\n",
    )
    c.sudo(
        f"certbot --nginx -d {SERVER_NAME}",
        watchers=[email_responder, tos_responder, redirect_responder],
    )


def install_docker(c):
    print("Installing Docker")
    c.sudo("apt update")
    c.sudo(
        "apt install apt-transport-https ca-certificates curl software-properties-common -y"
    )
    c.sudo(
        "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add -",
        pty=True,
    )
    c.sudo(
        'add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu bionic stable"'
    )
    c.sudo("apt update")
    c.run("apt-cache policy docker-ce")
    c.sudo("apt install docker-ce -y")

    docker_status = c.sudo("systemctl status docker")
    for line in docker_status.stdout.split("\n"):
        if "Active: active" in line:
            print("Docker is running")
            break
    else:
        print("Docker does not seem to be running: ", docker_status.stdout)
        print("...failed, exiting!")
        exit()

    print(f"Setting groups for user {c.user}")
    c.sudo("usermod -aG docker ${USER}")

    print("Installing docker-compose")

    # Get latest released version
    dc_version = requests.get("https://api.github.com/repos/docker/compose/releases")
    if dc_version.status_code != 200:
        print("Can't get current docker-compose version: ", dc_version.text)
        exit()
    dc_version = dc_version.json()[0]["tag_name"]

    c.sudo(
        f"curl -L https://github.com/docker/compose/releases/download/{dc_version}/docker-compose-`uname -s`-`uname -m` -o /usr/local/bin/docker-compose"
    )
    c.sudo("chmod +x /usr/local/bin/docker-compose")
    print("docker and docker-compose are installed")


def main():
    c = Connection(SERVER_NAME, config=CONFIG)
    basic_setup()
    install_nginx(c)
    install_lets_encrypt(c)
    install_docker(c)


if __name__ == "__main__":
    main()
