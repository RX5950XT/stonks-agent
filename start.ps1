[CmdletBinding()]
param(
    [ValidateSet("market", "paper", "research")]
    [string]$Mode = "research",

    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,

    [ValidateRange(1024, 65535)]
    [int]$DatabasePort = 55433,

    [ValidateRange(1024, 65535)]
    [int]$KronosPort = 17200,

    [switch]$NoBrowser,
    [switch]$SkipSync,
    [switch]$Check
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Stop-Launcher {
    param(
        [Parameter(Mandatory)]
        [string]$Message,
        [int]$Code = 1
    )

    [Console]::Error.WriteLine("stonks-start: $Message")
    exit $Code
}

function Assert-CommandAvailable {
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Stop-Launcher "required command not found: $Name"
    }
}

function Assert-SourceCheckout {
    $projectFile = Join-Path $PSScriptRoot "pyproject.toml"
    $composeFile = Join-Path $PSScriptRoot "infra\compose.gui.yaml"
    $missingProject = -not (Test-Path -LiteralPath $projectFile -PathType Leaf)
    $missingCompose = -not (Test-Path -LiteralPath $composeFile -PathType Leaf)
    if ($missingProject -or $missingCompose) {
        Stop-Launcher "run from a complete stonks-agent source checkout"
    }
}

function Assert-ResearchRuntime {
    if ($Mode -ne "research") {
        return
    }

    $kronosCompose = Join-Path $PSScriptRoot "infra\compose.kronos.yaml"
    $kronosManifest = Join-Path $PSScriptRoot "workers\kronos\model-manifest.json"
    $kronosModels = Join-Path $PSScriptRoot ".data\models\kronos"
    $missingKronosCompose = -not (
        Test-Path -LiteralPath $kronosCompose -PathType Leaf
    )
    $missingKronosManifest = -not (
        Test-Path -LiteralPath $kronosManifest -PathType Leaf
    )
    $missingKronosModels = -not (
        Test-Path -LiteralPath $kronosModels -PathType Container
    )
    if ($missingKronosCompose -or $missingKronosManifest -or $missingKronosModels) {
        Stop-Launcher -Message "Kronos CPU model or Compose runtime is incomplete" -Code 2
    }
}

function Assert-DockerComposeAvailable {
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Launcher "Docker Compose v2 is unavailable"
    }
}

function Assert-DockerDaemonAvailable {
    & docker info --format "{{.ServerVersion}}" *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Launcher "Docker Engine or Docker Desktop is not running"
    }
}

function New-GuiArguments {
    $arguments = @(
        "run",
        "--frozen",
        "stonks-gui",
        "serve",
        "--port",
        $Port.ToString()
    )
    if ($Mode -eq "paper") {
        $arguments += @("--with-paper", "--database-port", $DatabasePort.ToString())
    }
    elseif ($Mode -eq "research") {
        $arguments += @(
            "--with-research",
            "--database-port",
            $DatabasePort.ToString()
            "--kronos-port",
            $KronosPort.ToString()
        )
    }
    if ($NoBrowser) {
        $arguments += "--no-open-browser"
    }
    return $arguments
}

Assert-SourceCheckout
Assert-CommandAvailable "uv"
Assert-CommandAvailable "docker"
Assert-ResearchRuntime
Assert-DockerComposeAvailable
Assert-DockerDaemonAvailable

$guiArguments = New-GuiArguments
$displayCommand = (@("uv") + $guiArguments) -join " "
if ($Check) {
    Write-Output "mode=$Mode"
    Write-Output $displayCommand
    exit 0
}

Push-Location $PSScriptRoot
try {
    if (-not $SkipSync) {
        & uv sync --frozen
        if ($LASTEXITCODE -ne 0) {
            Stop-Launcher "uv sync --frozen failed"
        }
    }

    Write-Output "Starting Stonks Desk: mode=$Mode"
    & uv @guiArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
