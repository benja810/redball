<%
    from datetime import datetime
    import pytz
%>\
## Header: Team names with links to team subs and matchup image, followed by game date
${'# '}<%include file="matchup.mako" args="dateFormat='%a, %b %d'" />

## If game status is final, or live and not warmup, include boxscore, scoring plays, highlights, and linescore
% if (data[gamePk]['schedule']['status']['abstractGameCode'] == 'L' and data[gamePk]['schedule']['status']['statusCode'] != 'PW') or data[gamePk]['schedule']['status']['abstractGameCode'] == 'F':
<%include file="scoring_plays.mako" />

<%include file="highlights.mako" />
% endif

## configurable footer text
${settings.get('Highlight Thread',{}).get('FOOTER','')}
