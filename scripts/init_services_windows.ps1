#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PgHost = "127.0.0.1",
    [int]$PgPort = 5432,
    [string]$SuperUser = "postgres",
    [string]$SuperPassword = "123456",
    [string]$AppUser = "postgres",
    [string]$AppPassword = "123456",
    [string]$LangGraphDb = "langgraph_db",
    [string]$ChainlitDb = "chainlit_db",
    [string]$AppDir = "",
    [string]$EnvFile = "",
    [string]$PsqlPath = "",
    [string]$ChainlitDatalayerDir = "",
    [string]$ChainlitDatalayerGitUrl = "https://github.com/Chainlit/chainlit-datalayer.git",
    [string]$ChainlitDatalayerBranch = "main",
    [string]$MinioHost = "127.0.0.1",
    [int]$MinioApiPort = 9000,
    [int]$MinioConsolePort = 9001,
    [string]$MinioBucket = "chainlit-files",
    [string]$MinioRootUser = "minioadmin",
    [string]$MinioRootPassword = "minioadmin123",
    [string]$MinioBinDir = "",
    [string]$MinioDataDir = "",
    [string]$MinioDownloadUrl = "https://dl.min.io/server/minio/release/windows-amd64/minio.exe",
    [string]$McDownloadUrl = "https://dl.min.io/client/mc/release/windows-amd64/mc.exe",
    [int]$MinioStartTimeout = 15,
    [string]$PgRoot = "",
    [string]$PgVectorGitUrl = "https://github.com/pgvector/pgvector.git",
    [string]$PgVectorVersion = "v0.8.2",
    [string]$PgVectorSourceDir = "",
    [switch]$SkipPgVectorInstall,
    [switch]$SkipMinioSetup,
    [switch]$SkipChainlitMigration,
    [switch]$SkipPgVectorCheck
)

$ErrorActionPreference = "Stop"

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message"
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Resolve-DefaultAppDir {
    if ($AppDir) {
        return (Resolve-Path -LiteralPath $AppDir).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}

function Resolve-Psql {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        if (Test-Path -LiteralPath $ExplicitPath) {
            return (Resolve-Path -LiteralPath $ExplicitPath).Path
        }
        throw "psql.exe not found at: $ExplicitPath"
    }

    $cmd = Get-Command psql -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        Get-ChildItem -Path "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
    )
    if ($candidates.Count -gt 0) {
        return $candidates[0].FullName
    }

    throw "psql.exe not found. Add PostgreSQL bin to PATH or pass -PsqlPath."
}

function Resolve-AppPath {
    param(
        [string]$Value,
        [string]$DefaultRelativePath
    )

    $raw = $Value
    if (-not $raw) {
        $raw = $DefaultRelativePath
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($raw)
    if ([System.IO.Path]::IsPathRooted($expanded)) {
        return [System.IO.Path]::GetFullPath($expanded)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $script:ResolvedAppDir $expanded))
}

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMs = 500
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if (-not $task.Wait($TimeoutMs)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Ensure-DownloadedFile {
    param(
        [string]$Url,
        [string]$OutputPath,
        [string]$Name
    )

    if (Test-Path -LiteralPath $OutputPath) {
        Write-Info "$Name already exists: $OutputPath"
        return
    }

    $dir = Split-Path -Parent $OutputPath
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Write-Info "Downloading $Name to: $OutputPath"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Url -OutFile $OutputPath -UseBasicParsing
}

function Invoke-ExternalCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = ""
    )

    $oldLocation = Get-Location
    try {
        if ($WorkingDirectory) {
            Set-Location -LiteralPath $WorkingDirectory
        }
        $output = & $FilePath @Arguments 2>&1
        $text = ($output | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed: $FilePath $($Arguments -join ' ')`n$text"
        }
        return $text
    }
    finally {
        Set-Location -LiteralPath $oldLocation.Path
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Import-VsBuildEnvironment {
    if ((Get-Command nmake -ErrorAction SilentlyContinue) -and (Get-Command cl -ErrorAction SilentlyContinue)) {
        return
    }

    $vswhereCandidates = @()
    if (${env:ProgramFiles(x86)}) {
        $vswhereCandidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    }
    if ($env:ProgramFiles) {
        $vswhereCandidates += Join-Path $env:ProgramFiles "Microsoft Visual Studio\Installer\vswhere.exe"
    }
    $vswhereCandidates = $vswhereCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

    foreach ($vswhere in $vswhereCandidates) {
        $installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
        $installPath = ($installPath | Select-Object -First 1) -as [string]
        $installPath = if ($installPath) { $installPath.Trim() } else { "" }
        if (-not $installPath) {
            continue
        }

        $vcvars = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
        if (-not (Test-Path -LiteralPath $vcvars)) {
            continue
        }

        Write-Info "Loading Visual Studio x64 build environment: $vcvars"
        $envOutput = cmd /c "`"$vcvars`" >nul && set"
        foreach ($line in $envOutput) {
            if ($line -match "^(.*?)=(.*)$") {
                [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
            }
        }
        break
    }

    if (-not (Get-Command nmake -ErrorAction SilentlyContinue) -or -not (Get-Command cl -ErrorAction SilentlyContinue)) {
        throw "pgvector installation requires Visual Studio C++ build tools. Install Desktop development with C++ and run this script from an elevated x64 Native Tools prompt, or make nmake/cl available in PATH."
    }
}

function Test-PgVectorAvailable {
    $result = Invoke-Psql -Database $LangGraphDb -Command "SELECT 1 FROM pg_available_extensions WHERE name = 'vector';" -TuplesOnly
    return $result.Trim() -eq "1"
}

function Ensure-PgVectorSource {
    if (Test-Path -LiteralPath (Join-Path $script:ResolvedPgVectorSourceDir "Makefile.win")) {
        Write-Info "pgvector source already exists: $script:ResolvedPgVectorSourceDir"
        return
    }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        throw "git is required to clone pgvector. Install Git or pre-clone pgvector and pass -PgVectorSourceDir."
    }

    $parent = Split-Path -Parent $script:ResolvedPgVectorSourceDir
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Write-Info "Cloning pgvector ${PgVectorVersion}: $script:ResolvedPgVectorSourceDir"
    Invoke-ExternalCommand -FilePath "git" -Arguments @(
        "clone",
        "--depth", "1",
        "--branch", $PgVectorVersion,
        $PgVectorGitUrl,
        $script:ResolvedPgVectorSourceDir
    ) | Out-Null
}

function Ensure-PgVectorInstalled {
    if ($SkipPgVectorInstall) {
        Write-Info "Skipping pgvector installation."
        return
    }

    if (Test-PgVectorAvailable) {
        Write-Info "pgvector is already available in PostgreSQL."
        return
    }

    if (-not (Test-IsAdministrator)) {
        throw "pgvector installation writes to the PostgreSQL installation directory. Re-run this script as Administrator or pre-install pgvector."
    }

    if (-not (Test-Path -LiteralPath $script:ResolvedPgRoot)) {
        throw "PostgreSQL root not found: $script:ResolvedPgRoot"
    }

    Import-VsBuildEnvironment
    Ensure-PgVectorSource

    $oldPgRoot = [Environment]::GetEnvironmentVariable("PGROOT", "Process")
    [Environment]::SetEnvironmentVariable("PGROOT", $script:ResolvedPgRoot, "Process")
    try {
        Write-Info "Building pgvector with nmake (PGROOT=$script:ResolvedPgRoot)"
        Invoke-ExternalCommand -FilePath "nmake" -Arguments @("/F", "Makefile.win") -WorkingDirectory $script:ResolvedPgVectorSourceDir | Out-Null
        Write-Info "Installing pgvector into PostgreSQL"
        Invoke-ExternalCommand -FilePath "nmake" -Arguments @("/F", "Makefile.win", "install") -WorkingDirectory $script:ResolvedPgVectorSourceDir | Out-Null
    }
    finally {
        if ($null -eq $oldPgRoot) {
            [Environment]::SetEnvironmentVariable("PGROOT", $null, "Process")
        }
        else {
            [Environment]::SetEnvironmentVariable("PGROOT", $oldPgRoot, "Process")
        }
    }

    if (-not (Test-PgVectorAvailable)) {
        throw "pgvector install completed but PostgreSQL still does not list extension 'vector'. Check PostgreSQL version and install directory."
    }
}

function Quote-PgIdentifier {
    param([string]$Value)
    return '"' + ($Value -replace '"', '""') + '"'
}

function Quote-PgLiteral {
    param([string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function ConvertTo-UrlComponent {
    param([string]$Value)
    return [System.Uri]::EscapeDataString($Value)
}

function New-PostgresUrl {
    param([string]$Database)
    $user = ConvertTo-UrlComponent $AppUser
    $password = ConvertTo-UrlComponent $AppPassword
    return "postgresql://${user}:${password}@${PgHost}:${PgPort}/${Database}"
}

function Invoke-Psql {
    param(
        [string]$Database = "postgres",
        [string]$Command,
        [switch]$TuplesOnly,
        [switch]$AllowFailure
    )

    $oldPassword = [Environment]::GetEnvironmentVariable("PGPASSWORD", "Process")
    [Environment]::SetEnvironmentVariable("PGPASSWORD", $SuperPassword, "Process")

    try {
        $args = @(
            "-h", $PgHost,
            "-p", [string]$PgPort,
            "-U", $SuperUser,
            "-d", $Database,
            "-v", "ON_ERROR_STOP=1",
            "-X"
        )
        if ($TuplesOnly) {
            $args += @("-t", "-A")
        }
        if ($Command) {
            $args += @("-c", $Command)
        }

        $output = & $script:PsqlExe @args 2>&1
        $text = ($output | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
            throw "psql failed on database '$Database'.`n$text"
        }
        return $text
    }
    finally {
        if ($null -eq $oldPassword) {
            [Environment]::SetEnvironmentVariable("PGPASSWORD", $null, "Process")
        }
        else {
            [Environment]::SetEnvironmentVariable("PGPASSWORD", $oldPassword, "Process")
        }
    }
}

function Ensure-Role {
    $roleIdent = Quote-PgIdentifier $AppUser
    $userLit = Quote-PgLiteral $AppUser
    $passLit = Quote-PgLiteral $AppPassword
    $dollar = '$$'
    $sql = @(
        "DO $dollar",
        "BEGIN",
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $userLit) THEN",
        "    CREATE ROLE $roleIdent LOGIN PASSWORD $passLit;",
        "  ELSE",
        "    ALTER ROLE $roleIdent WITH LOGIN PASSWORD $passLit;",
        "  END IF;",
        "END",
        "$dollar;"
    ) -join [Environment]::NewLine

    Write-Info "Creating/updating PostgreSQL role: $AppUser"
    Invoke-Psql -Database "postgres" -Command $sql | Out-Null
}

function Ensure-Database {
    param([string]$DatabaseName)

    $dbIdent = Quote-PgIdentifier $DatabaseName
    $dbLit = Quote-PgLiteral $DatabaseName
    $roleIdent = Quote-PgIdentifier $AppUser
    $exists = Invoke-Psql -Database "postgres" -Command "SELECT 1 FROM pg_database WHERE datname = $dbLit;" -TuplesOnly

    if ($exists.Trim() -eq "1") {
        Write-Info "Database exists: $DatabaseName"
        Invoke-Psql -Database "postgres" -Command "ALTER DATABASE $dbIdent OWNER TO $roleIdent;" | Out-Null
    }
    else {
        Write-Info "Creating database: $DatabaseName"
        Invoke-Psql -Database "postgres" -Command "CREATE DATABASE $dbIdent OWNER $roleIdent;" | Out-Null
    }

    Invoke-Psql -Database $DatabaseName -Command "GRANT ALL ON SCHEMA public TO $roleIdent; ALTER SCHEMA public OWNER TO $roleIdent;" | Out-Null
}

function Ensure-Extension {
    param(
        [string]$DatabaseName,
        [string]$ExtensionName,
        [string]$FailureHint
    )

    $extensionIdent = Quote-PgIdentifier $ExtensionName
    $output = Invoke-Psql -Database $DatabaseName -Command "CREATE EXTENSION IF NOT EXISTS $extensionIdent;" -AllowFailure
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Cannot create extension '$ExtensionName' in '$DatabaseName'. $FailureHint"
        if ($output) {
            Write-Warn $output
        }
    }
    else {
        Write-Info "Extension ready in ${DatabaseName}: $ExtensionName"
    }
}

function Test-ChainlitDatalayerDir {
    param([string]$Path)
    if (-not $Path) {
        return $false
    }
    return (
        (Test-Path -LiteralPath (Join-Path $Path "package.json")) -and
        (Test-Path -LiteralPath (Join-Path $Path "prisma\schema.prisma"))
    )
}

function Ensure-ChainlitDatalayerDir {
    $rootDir = Split-Path -Parent $script:ResolvedAppDir
    $candidates = @()
    if ($ChainlitDatalayerDir) {
        $candidates += $ChainlitDatalayerDir
    }
    $candidates += (Join-Path $rootDir "chainlit-datalayer")
    $candidates += (Join-Path $script:ResolvedAppDir "chainlit-datalayer")

    foreach ($candidate in $candidates) {
        if (Test-ChainlitDatalayerDir $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $target = Join-Path $rootDir "chainlit-datalayer"
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        throw "chainlit-datalayer not found and git is unavailable. Clone it to '$target' or pass -ChainlitDatalayerDir."
    }

    Write-Info "chainlit-datalayer not found. Cloning to: $target"
    & git clone --depth 1 --branch $ChainlitDatalayerBranch $ChainlitDatalayerGitUrl $target
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed for chainlit-datalayer."
    }
    return (Resolve-Path -LiteralPath $target).Path
}

function Invoke-ChainlitMigration {
    $datalayerDir = Ensure-ChainlitDatalayerDir
    Write-Info "Running Chainlit Prisma migration in: $datalayerDir"

    $npm = Get-Command npm -ErrorAction SilentlyContinue
    $npx = Get-Command npx -ErrorAction SilentlyContinue
    if (-not $npm -or -not $npx) {
        throw "Node.js/npm/npx is required for Chainlit Prisma migration."
    }

    Push-Location $datalayerDir
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $datalayerDir "node_modules"))) {
            if (Test-Path -LiteralPath (Join-Path $datalayerDir "package-lock.json")) {
                & npm ci
            }
            else {
                & npm install
            }
            if ($LASTEXITCODE -ne 0) {
                throw "npm dependency installation failed."
            }
        }

        $oldDatabaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
        [Environment]::SetEnvironmentVariable("DATABASE_URL", (New-PostgresUrl $ChainlitDb), "Process")
        try {
            & npx prisma migrate deploy
            if ($LASTEXITCODE -ne 0) {
                throw "npx prisma migrate deploy failed."
            }
        }
        finally {
            if ($null -eq $oldDatabaseUrl) {
                [Environment]::SetEnvironmentVariable("DATABASE_URL", $null, "Process")
            }
            else {
                [Environment]::SetEnvironmentVariable("DATABASE_URL", $oldDatabaseUrl, "Process")
            }
        }
    }
    finally {
        Pop-Location
    }
}

function Wait-LocalMinioReady {
    $healthUrl = "http://${MinioHost}:${MinioApiPort}/minio/health/live"
    $deadline = (Get-Date).AddSeconds($MinioStartTimeout)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
            if ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 500) {
                Write-Info "MinIO is ready: http://${MinioHost}:${MinioApiPort}"
                return
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "MinIO did not become ready within ${MinioStartTimeout}s. Check port ${MinioApiPort} and MinIO logs."
}

function Start-LocalMinio {
    if (Test-TcpPort -HostName $MinioHost -Port $MinioApiPort -TimeoutMs 500) {
        Write-Info "MinIO API port is already open: ${MinioHost}:${MinioApiPort}"
        return
    }

    New-Item -ItemType Directory -Force -Path $script:ResolvedMinioDataDir | Out-Null

    Write-Info "Starting local MinIO"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:ResolvedMinioExe
    $psi.Arguments = "server `"$script:ResolvedMinioDataDir`" --address `":$MinioApiPort`" --console-address `":$MinioConsolePort`""
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables["MINIO_ROOT_USER"] = $MinioRootUser
    $psi.EnvironmentVariables["MINIO_ROOT_PASSWORD"] = $MinioRootPassword

    $process = [System.Diagnostics.Process]::Start($psi)
    if (-not $process) {
        throw "Failed to start MinIO."
    }
    Write-Info "MinIO process started: pid=$($process.Id)"
    Wait-LocalMinioReady
}

function Invoke-Mc {
    param([string[]]$Arguments)

    $output = & $script:ResolvedMcExe @Arguments 2>&1
    $text = ($output | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "mc command failed: mc $($Arguments -join ' ')`n$text"
    }
    return $text
}

function Ensure-MinioBucket {
    $aliasName = "mcp-alint-local"
    $endpoint = "http://${MinioHost}:${MinioApiPort}"
    Write-Info "Configuring MinIO bucket: $MinioBucket"
    try {
        Invoke-Mc -Arguments @("alias", "set", $aliasName, $endpoint, $MinioRootUser, $MinioRootPassword) | Out-Null
        Invoke-Mc -Arguments @("mb", "--ignore-existing", "${aliasName}/${MinioBucket}") | Out-Null
    }
    finally {
        try {
            Invoke-Mc -Arguments @("alias", "remove", $aliasName) | Out-Null
        }
        catch {
            # Alias cleanup is best-effort.
        }
    }
}

function Ensure-Minio {
    if ($SkipMinioSetup) {
        Write-Info "Skipping MinIO setup."
        return
    }

    Ensure-DownloadedFile -Url $MinioDownloadUrl -OutputPath $script:ResolvedMinioExe -Name "minio.exe"
    Ensure-DownloadedFile -Url $McDownloadUrl -OutputPath $script:ResolvedMcExe -Name "mc.exe"
    Start-LocalMinio
    Ensure-MinioBucket
}

$script:ResolvedAppDir = Resolve-DefaultAppDir
if (-not $EnvFile) {
    $EnvFile = Join-Path $script:ResolvedAppDir ".env"
}
$script:ResolvedEnvFile = $EnvFile
if (-not (Test-Path -LiteralPath $script:ResolvedEnvFile)) {
    throw ".env not found at '$script:ResolvedEnvFile'. Create it from .env.example and edit it before running this script."
}
$script:PsqlExe = Resolve-Psql $PsqlPath
$script:ResolvedPgRoot = if ($PgRoot) {
    Resolve-AppPath -Value $PgRoot -DefaultRelativePath ""
} else {
    Split-Path -Parent (Split-Path -Parent $script:PsqlExe)
}
$script:ResolvedPgVectorSourceDir = Resolve-AppPath -Value $PgVectorSourceDir -DefaultRelativePath (Join-Path ".local\pgvector" $PgVectorVersion)
$script:ResolvedMinioBinDir = Resolve-AppPath -Value $MinioBinDir -DefaultRelativePath ".local\minio\bin"
$script:ResolvedMinioDataDir = Resolve-AppPath -Value $MinioDataDir -DefaultRelativePath ".local\minio\data"
$script:ResolvedMinioExe = Join-Path $script:ResolvedMinioBinDir "minio.exe"
$script:ResolvedMcExe = Join-Path $script:ResolvedMinioBinDir "mc.exe"

Write-Info "Project directory: $script:ResolvedAppDir"
Write-Info "Using psql: $script:PsqlExe"
Write-Info "PostgreSQL root: $script:ResolvedPgRoot"
if (-not $SkipPgVectorInstall) {
    Write-Info "pgvector source directory: $script:ResolvedPgVectorSourceDir"
}
if (-not $SkipMinioSetup) {
    Write-Info "MinIO bin directory: $script:ResolvedMinioBinDir"
    Write-Info "MinIO data directory: $script:ResolvedMinioDataDir"
}

Invoke-Psql -Database "postgres" -Command "SELECT current_database(), current_user;" | Out-Null
Ensure-Role
Ensure-Database $LangGraphDb
Ensure-Database $ChainlitDb

Ensure-Extension -DatabaseName $ChainlitDb -ExtensionName "pgcrypto" -FailureHint "Chainlit migration also tries to create it; install PostgreSQL contrib components if this keeps failing."
if (-not $SkipPgVectorCheck) {
    Ensure-PgVectorInstalled
    Ensure-Extension -DatabaseName $LangGraphDb -ExtensionName "vector" -FailureHint "Install pgvector for semantic long-term memory, or set MEMORY_ENABLE_SEMANTIC_SEARCH=false."
}

if (-not $SkipChainlitMigration) {
    Invoke-ChainlitMigration
}
else {
    Write-Info "Skipping Chainlit Prisma migration."
}

Ensure-Minio

Write-Info "Connectivity checks"
Invoke-Psql -Database $LangGraphDb -Command "SELECT current_database(), current_user;" | Write-Host
Invoke-Psql -Database $ChainlitDb -Command "SELECT current_database(), current_user;" | Write-Host
if (-not $SkipMinioSetup) {
    Invoke-Mc -Arguments @("alias", "set", "mcp-alint-check", "http://${MinioHost}:${MinioApiPort}", $MinioRootUser, $MinioRootPassword) | Out-Null
    Invoke-Mc -Arguments @("ls", "mcp-alint-check/${MinioBucket}") | Write-Host
    Invoke-Mc -Arguments @("alias", "remove", "mcp-alint-check") | Out-Null
}

Write-Host ""
Write-Host "[DONE] Local services initialized."
Write-Host "       LangGraph DB: $LangGraphDb"
Write-Host "       Chainlit DB:  $ChainlitDb"
Write-Host "       App user:     $AppUser"
if (-not $SkipMinioSetup) {
    Write-Host "       MinIO API:    http://${MinioHost}:${MinioApiPort}"
    Write-Host "       MinIO bucket: $MinioBucket"
}
