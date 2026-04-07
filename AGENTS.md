# AGENTS.md

## project summary
- This project is a team-based game in which players form teams, and the team that uses generative AI to create the image most similar to the presented reference image wins.
- Players form teams, and the team that creates the image most similar to the given reference image wins.
- When the server starts, it prompts for the API key required to call the AI model. Since the project currently uses the Gemini model, it uses an API key issued by Google AI Studio.
- At the end of the game, awards are presented by team.

## game flow
- Players and managers have different permissions and different screens.
- Players are assigned to teams by a manager.
- The manager is responsible for starting the game, presenting the reference image, requesting AI judgment when needed or when a manual check fails, entering scores, and advancing to the next round.
- Players are responsible for entering team prompts, generating images, and submitting their final drawings.

## rules
- Do not change line endings unless necessary.
- Do not use `localStorage` in this project.

## encoding
- Use UTF-8 for all files by default.
- Never change file encoding unless explicitly required.
- If a file's encoding is unclear, do not rewrite it.
