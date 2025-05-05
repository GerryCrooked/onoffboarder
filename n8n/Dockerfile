FROM n8nio/n8n

# Install PowerShell Core + ActiveDirectory module via Linux-Tools
USER root

RUN apt-get update && \
    apt-get install -y wget apt-transport-https software-properties-common && \
    wget -q https://packages.microsoft.com/config/debian/11/packages-microsoft-prod.deb && \
    dpkg -i packages-microsoft-prod.deb && \
    apt-get update && \
    apt-get install -y powershell

# Install PowerShell modules
RUN pwsh -Command "Install-Module -Name ActiveDirectory -Force -Scope AllUsers"

# Use normal n8n user again
USER node
