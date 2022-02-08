/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use crate::ArgExtension;
use anyhow::Result;
use clap::Args;
use environment::MononokeEnvironment;

/// Command line arguments that affect mcrouter usage
#[derive(Args, Debug)]
pub struct McrouterArgs {
    /// Use local McRouter for rate limits
    #[clap(long)]
    pub enable_mcrouter: bool,
}

pub struct McrouterArgExtension;

impl ArgExtension for McrouterArgExtension {
    type Args = McrouterArgs;

    fn process_args(&self, args: &McrouterArgs, env: &mut MononokeEnvironment) -> Result<()> {
        if !args.enable_mcrouter {
            return Ok(());
        }

        #[cfg(fbcode_build)]
        {
            ::ratelim::use_proxy_if_available(env.fb);
            return Ok(());
        }

        #[cfg(not(fbcode_build))]
        {
            unimplemented!("Passed --enable-mcrouter but it is supported only for fbcode builds",);
        }
    }
}
