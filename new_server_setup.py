import os
import re
import sys

from io import BytesIO, StringIO
from string import Template

import getpass
from fabric import Config, Connection
from invoke import Responder

SERVER_NAME = "staging.skysmuggler.com"
EMAIL_ADDRESS = "anson.vandoren@gmail.com"
DO_HTTPS_REDIRECT = True
# If you don't want to type your sudo password each time, you can set it via environment variable instead
sudo_pass = os.environ.get("FABRIC_SUDO_PASSWORD", None)
if sudo_pass is None:
    sudo_pass = getpass.getpass("Enter sudo password: ")

CONFIG = Config(overrides={"sudo": {"password": sudo_pass}})


def install_nginx(c):
    """https://www.digitalocean.com/community/tutorials/how-to-install-nginx-on-ubuntu-18-04"""
    print("Checking for Nginx...")
    nginx_installed = c.run("dpkg -s nginx")
    for line in nginx_installed.stdout.split("\n"):
        if "Status: install ok installed" in line:
            nginx_installed = True
            break
    else:
        nginx_installed = False

    if not nginx_installed:
        print("\tInstalling Nginx...")
        c.sudo("apt update")
        c.sudo("apt install nginx")
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
    if not c.run(f"cat /etc/nginx/sites-enabled/{SERVER_NAME}"):
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
    c.sudo("add-apt-repository ppa:certbot/certbot -y")
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


def main():
    c = Connection(SERVER_NAME, config=CONFIG)
    install_nginx(c)
    install_lets_encrypt(c)


if __name__ == "__main__":
    main()
