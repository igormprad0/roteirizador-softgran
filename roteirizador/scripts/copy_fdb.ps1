# Copia as duas bases dos clientes para data/fdb/ com nome ASCII.
# O nome original tem cedilha, e isso quebra o isql e o mount do container.
#
# Por padrao procura os .fdb na pasta acima do repositorio. Em outra maquina,
# passe onde eles estao:
#   powershell -File scripts\copy_fdb.ps1 -Src "D:\bases"
param(
    [string]$Src = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$raiz = Split-Path -Parent $PSScriptRoot
$dst  = Join-Path $raiz 'data'
$dst  = Join-Path $dst 'fdb'
New-Item -ItemType Directory -Force $dst | Out-Null

$origens = @(
    @{ De = "cliente loca$([char]0xE7)$([char]0xE3)o.FDB"; Para = 'locacao.fdb' },
    @{ De = 'cliente entrega posterior.FDB';              Para = 'entrega_posterior.fdb' }
)

foreach ($o in $origens) {
    $caminho = Join-Path $Src $o.De
    if (-not (Test-Path -LiteralPath $caminho)) {
        Write-Error "Nao encontrei '$($o.De)' em '$Src'. Use -Src para apontar a pasta das bases."
        exit 1
    }
    Copy-Item -LiteralPath $caminho (Join-Path $dst $o.Para) -Force
}

Get-ChildItem $dst | Select-Object Name, @{n='GB';e={[math]::Round($_.Length/1GB,2)}}
Write-Host ''
Write-Host 'Proximo passo: ./scripts/grant_rotas.sh (com a stack parada)'
