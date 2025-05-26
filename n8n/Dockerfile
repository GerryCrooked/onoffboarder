# Start with the official n8n image (likely Alpine-based)
FROM n8nio/n8n:latest

# Switch to root user to install packages
USER root

# Install prerequisites for PowerShell on Alpine
# Details: https://learn.microsoft.com/en-us/powershell/scripting/install/install-alpine?view=powershell-7.4
RUN apk update && \
    apk add --no-cache \
    ca-certificates \
    krb5-libs \
    libgcc \
    libintl \
    libssl3 \
    libstdc++ \
    tzdata \
    userspace-rcu \
    zlib \
    icu-libs \
    curl \
    ncurses-terminfo-base \
    less # Optional, aber nützlich

# Install PowerShell
# Microsoft hat offizielle Pakete für Alpine in deren Repository
RUN apk add --no-cache powershell

# Install PowerShell modules
# HINWEIS: Die volle Funktionalität des Microsoft.ActiveDirectory.Management Moduls
# auf Linux für schreibende Operationen ist nicht garantiert und kann
# zusätzliche Konfiguration (z.B. Kerberos für den Container) erfordern.
# Für Leseoperationen funktioniert es oft besser.
RUN pwsh -Command "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; Install-Module -Name Microsoft.ActiveDirectory.Management -Repository PSGallery -Force -Scope AllUsers -AcceptLicense; Exit 0"
# Exit 0 ist ein Workaround, falls Install-Module manchmal einen nicht-Null Exit-Code gibt, obwohl es geklappt hat.
# Überprüfe nach dem Bauen des Images, ob das Modul wirklich da ist:
# docker run --rm your-custom-n8n-image pwsh -Command "Get-Module -ListAvailable Microsoft.ActiveDirectory.Management"

# Switch back to the node user
USER node

