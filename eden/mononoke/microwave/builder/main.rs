/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

mod filenodes;

use ::filenodes::Filenodes;
use anyhow::{format_err, Error};
use blobrepo::DangerousOverride;
use blobrepo_factory::open_blobrepo;
use clap::{Arg, ArgMatches, SubCommand};
use cloned::cloned;
use cmdlib::{args, monitoring::AliveService};
use context::SessionContainer;
use fbinit::FacebookInit;
use futures::{channel::mpsc, compat::Future01CompatExt, future};
use metaconfig_parser::RepoConfigs;
use metaconfig_types::RepoConfig;
use microwave::{Snapshot, SnapshotLocation};
use slog::{info, o, Logger};
use std::path::Path;
use std::sync::Arc;

use crate::filenodes::MicrowaveFilenodes;

const SUBCOMMAND_LOCAL_PATH: &str = "local-path";
const ARG_LOCAL_PATH: &str = "local-path";

const SUBCOMMAND_BLOBSTORE: &str = "blobstore";

async fn do_main<'a>(
    fb: FacebookInit,
    matches: &ArgMatches<'a>,
    logger: &Logger,
) -> Result<(), Error> {
    let mut scuba = args::get_scuba_sample_builder(fb, &matches)?;
    scuba.add_common_server_data();

    let mysql_options = cmdlib::args::parse_mysql_options(&matches);
    let readonly_storage = cmdlib::args::parse_readonly_storage(&matches);
    let blobstore_options = cmdlib::args::parse_blobstore_options(&matches);
    let caching = cmdlib::args::init_cachelib(fb, &matches, None);

    let RepoConfigs { repos, common } = args::read_configs(fb, &matches)?;
    let scuba_censored_table = common.scuba_censored_table;

    let location = match matches.subcommand() {
        (SUBCOMMAND_LOCAL_PATH, Some(sub)) => {
            let path = Path::new(sub.value_of_os(ARG_LOCAL_PATH).unwrap());
            info!(logger, "Writing to path {}", path.display());
            SnapshotLocation::SharedLocalPath(path)
        }
        (SUBCOMMAND_BLOBSTORE, Some(_)) => SnapshotLocation::Blobstore,
        (name, _) => return Err(format_err!("Invalid subcommand: {:?}", name)),
    };

    let futs = repos
        .into_iter()
        .map(|(name, config)| {
            cloned!(blobstore_options, scuba_censored_table, mut scuba);

            async move {
                let logger = logger.new(o!("repo" => name.clone()));

                let ctx = {
                    scuba.add("reponame", name);
                    let session = SessionContainer::new_with_defaults(fb);
                    session.new_context(logger.clone(), scuba)
                };

                let (filenodes_sender, filenodes_receiver) = mpsc::channel(1000);
                let warmup_ctx = ctx.clone();

                let RepoConfig {
                    storage_config,
                    repoid,
                    bookmarks_cache_ttl,
                    redaction,
                    filestore,
                    derived_data_config,
                    cache_warmup,
                    ..
                } = config;

                let warmup = async move {
                    let repo = open_blobrepo(
                        fb,
                        storage_config,
                        repoid,
                        mysql_options,
                        caching,
                        bookmarks_cache_ttl,
                        redaction,
                        scuba_censored_table,
                        filestore,
                        readonly_storage,
                        blobstore_options,
                        logger,
                        derived_data_config,
                    )
                    .compat()
                    .await?;

                    let warmup_repo = repo.dangerous_override(|inner| -> Arc<dyn Filenodes> {
                        Arc::new(MicrowaveFilenodes::new(repoid, filenodes_sender, inner))
                    });

                    cache_warmup::cache_warmup(warmup_ctx, warmup_repo, cache_warmup)
                        .compat()
                        .await?;

                    Result::<_, Error>::Ok(repo)
                };

                let handle = tokio::task::spawn(warmup);
                let snapshot = Snapshot::build(filenodes_receiver).await;

                // Make sure cache warmup has succeeded before committign this snapshot, and get
                // the repo back.
                let repo = handle.await??;

                snapshot.commit(&ctx, &repo, location).await?;

                Result::<_, Error>::Ok(())
            }
        })
        .collect::<Vec<_>>();

    future::try_join_all(futs).await?;

    Ok(())
}

#[fbinit::main]
fn main(fb: FacebookInit) -> Result<(), Error> {
    let app = args::MononokeApp::new("Mononoke Local Replay")
        .with_advanced_args_hidden()
        .with_fb303_args()
        .with_all_repos()
        .with_scuba_logging_args()
        .build()
        .subcommand(
            SubCommand::with_name(SUBCOMMAND_LOCAL_PATH)
                .about("Write cache priming data to path")
                .arg(
                    Arg::with_name(ARG_LOCAL_PATH)
                        .takes_value(true)
                        .required(true),
                ),
        )
        .subcommand(
            SubCommand::with_name(SUBCOMMAND_BLOBSTORE)
                .about("Write cache priming data to the repository blobstore"),
        );

    let matches = app.get_matches();

    let logger = args::init_logging(fb, &matches);

    let main = do_main(fb, &matches, &logger);

    cmdlib::helpers::block_execute(main, fb, "microwave", &logger, &matches, AliveService)?;

    Ok(())
}
