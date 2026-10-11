A tool that helps me save my desktop session between reboots, and to rearrange
my windows between monitors.

I use Ghostty and Nautilus. Ghostty on Linux lacks the window-save-state feature
so I had to hack an attempt to replicate it.

## Usage

```
niri-layout-manager --help
```

## Development

```
uv tool install --editable .
alias ,layout=niri-layout-manager
```
