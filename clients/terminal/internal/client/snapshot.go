package client

// Durable and operational snapshots are fetched as two serialized operations;
// the application state cannot become Ready until both are installed.
