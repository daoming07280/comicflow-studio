param([Parameter(Mandatory=$true)][string]$InputJson)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $installed = @($voice.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh*' })
    if ($installed.Count -eq 0) { throw 'No Chinese Windows voice installed. Please select Edge TTS.' }
    $voice.SelectVoice($installed[0].VoiceInfo.Name)
    $voice.Rate = [int]$config.rate
    $voice.SetOutputToWaveFile([string]$config.output)
    $voice.Speak([string]$config.text)
} finally { $voice.Dispose() }
