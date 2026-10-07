import { Container, getContainer } from "@cloudflare/containers";

// Front door for the download engine. Every request goes to the same named
// container instance (getContainer's default name): the task registry lives in
// the container process's memory, so /download, /status and /file calls must
// all reach that one instance.
export class DownloaderContainer extends Container {
	defaultPort = 8000;
	// Must outlast a download+merge that is running between status polls.
	// The container readiness probe hits PingEndpoint's default path, /ping,
	// which the FastAPI app serves.
	sleepAfter = "10m";

	constructor(ctx, env) {
		super(ctx, env);
		// Worker vars and secrets are read here and forwarded to the container
		// process as environment variables.
		this.envVars = {
			DOWNLOAD_DIR: env.DOWNLOAD_DIR ?? "/tmp/ytdl",
			YTDLP_PLAYER_CLIENTS: env.YTDLP_PLAYER_CLIENTS ?? "tv_embedded",
			RATE_LIMIT: env.RATE_LIMIT ?? "20/minute",
			API_KEYS: env.API_KEYS ?? '["change-me-in-production"]',
			CORS_ORIGINS: env.CORS_ORIGINS ?? '["http://localhost:3000"]',
		};
	}
}

export default {
	async fetch(request, env) {
		return getContainer(env.MY_CONTAINER).fetch(request);
	},
};
